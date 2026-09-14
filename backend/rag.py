# -*- coding: utf-8 -*-
"""RAG 模块：文档加载/去重/清洗/切分、BM25 + 向量双路召回、重排序、带证据回答。"""
import os
import re
import json
import asyncio
import hashlib
from datetime import datetime
from collections import defaultdict

import jieba
import pytesseract
from PIL import Image
from rank_bm25 import BM25Okapi
from langchain_community.document_loaders import TextLoader, PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from sentence_transformers import CrossEncoder
from langchain_core.documents import Document

from .db import cache_get, cache_set
from .config import llm

# ---------- 1. 加载文档（支持 txt, pdf, docx, 图片 OCR；所有类型统一数据清洗） ----------
def clean_text(text):
    """统一数据清洗：去控制字符/乱码、合并连续空白与全角空格、合并多余空行"""
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# ---------- 文档去重（加载时）：MD5 精确去重 + MinHash 近似去重 ----------
seen_hashes = set()      # 文件 MD5，抓"完全相同"（改名/复制）
size_buckets = defaultdict(list)  # 按文件大小分桶，桶内做 MinHash 近似去重，避免全量两两比较

def _file_md5(path):
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()

def _minhash(text, k=64):
    """简化 MinHash：对文档切词，取 k 个确定性哈希的最小值作签名（无需随机排列，确定性可复现）"""
    toks = set(jieba.cut(text))
    return [min(((a * 31 + 7) * hash(t)) % 2147483647 for t in toks) if toks else 0 for a in range(1, k + 1)]

def _jaccard(s1, s2):
    return sum(a == b for a, b in zip(s1, s2)) / len(s1)

def _is_near_dup(text, size):
    """同大小桶内 MinHash 相似度 >=0.85 视为近似重复（同一文档不同版本/转载）"""
    for other in size_buckets.get(size // 1024, []):
        if _jaccard(_minhash(text), _minhash(other)) >= 0.85:
            return True
    return False

documents = []
data_dir = "data"
for root, dirs, files in os.walk(data_dir):
    for file in files:
        file_path = os.path.join(root, file)
        # 文件级去重：完全相同（MD5）直接跳过
        if _file_md5(file_path) in seen_hashes:
            print(f"跳过重复文件（MD5）：{file_path}")
            continue
        seen_hashes.add(_file_md5(file_path))
        # 文档日期元数据：优先取文件名中的 4 位年份，否则用文件修改时间（整数 YYYYMMDD，Chroma 的 $gte 只支持数值）
        mtime_int = int(datetime.fromtimestamp(os.path.getmtime(file_path)).strftime("%Y%m%d"))
        year_match = re.search(r"(20\d{2})", file)
        doc_date = int(year_match.group(1) + "0101") if year_match else mtime_int
        if file.endswith((".txt", ".md")):
            loader = TextLoader(file_path, encoding="UTF-8")
            for d in loader.load():
                d.page_content = clean_text(d.page_content)
                d.metadata["date"] = doc_date
                documents.append(d)
        elif file.endswith(".pdf"):
            loader = PyPDFLoader(file_path)
            for d in loader.load():
                d.page_content = clean_text(d.page_content)
                d.metadata["date"] = doc_date
                documents.append(d)
        elif file.endswith(".docx"):
            # Word 文档：段落 + 表格（表格内容用 " | " 分隔转文本，避免丢失）
            from docx import Document as DocxDocument
            doc = DocxDocument(file_path)
            parts = [p.text for p in doc.paragraphs if p.text.strip()]
            for table in doc.tables:
                for row in table.rows:
                    parts.append(" | ".join(c.text.strip() for c in row.cells))
            text = clean_text("\n".join(parts))
            if text:
                documents.append(Document(page_content=text, metadata={"source": file_path, "date": doc_date}))
        elif file.lower().endswith((".png", ".jpg", ".jpeg", ".bmp", ".tiff")):
            # 图片处理：OCR 提取文本
            try:
                image = Image.open(file_path)
                text = pytesseract.image_to_string(image, lang="chi_sim+eng", config="--tessdata-dir resources/tessdata")
                if text.strip():
                    documents.append(Document(page_content=clean_text(text), metadata={"source": file_path, "date": doc_date}))
            except Exception as e:
                print(f"OCR failed for {file_path}: {e}")
# 近似去重：加载完成后按内容 MinHash 再滤一遍（改过内容但同源的文件）
unique_documents = []
for doc in documents:
    if not _is_near_dup(doc.page_content, len(doc.page_content)):
        unique_documents.append(doc)
    else:
        print(f"跳过近似重复文档：{doc.metadata.get('source')}")
documents = unique_documents

# ---------- 2. 切分文档 ----------
text_splitter = RecursiveCharacterTextSplitter(
    chunk_size=500,
    chunk_overlap=50,
    separators=["\n\n", "\n", "。", "！", "？", "；", " ", ""]
)
docs = text_splitter.split_documents(documents)
# 每个块打上全局索引，(source, chunk_index) 唯一定位原文，作为回答的证据
for i, chunk in enumerate(docs):
    chunk.metadata["chunk_index"] = i
print(f"切分后文档块数：{len(docs)}")

# ---------- 3. 构建 BM25 索引（必须在 docs 生成之后） ----------
tokenized_corpus = [list(jieba.cut(doc.page_content)) for doc in docs]
bm25 = BM25Okapi(tokenized_corpus)

# ---------- 4. 文本向量化与向量数据库 ----------
embedding_model_name = "BAAI/bge-small-zh-v1.5"
embeddings = HuggingFaceEmbeddings(
    model_name=embedding_model_name,
    model_kwargs={'device': 'cpu'},
    encode_kwargs={'normalize_embeddings': True}
)

# 检查是否已存在向量库：存在则增量加入新文档（data/ 新增文件后重启即可入库），不存在则新建
persist_dir = "./chroma_db"
if os.path.exists(persist_dir):
    vectorstore = Chroma(
        persist_directory=persist_dir,
        embedding_function=embeddings,
        collection_name="pet_health"
    )
    existing_sources = {m.get("source") for m in (vectorstore.get().get("metadatas") or [])}
    new_docs = [d for d in docs if d.metadata.get("source") not in existing_sources]
    if new_docs:
        vectorstore.add_documents(new_docs)
        print(f"向量库新增 {len(new_docs)} 个文档块")
else:
    vectorstore = Chroma.from_documents(
        documents=docs,
        embedding=embeddings,
        persist_directory=persist_dir,
        collection_name="pet_health"
    )

# ---------- 5. 多路召回函数（向量 + BM25 + RRF 融合；min_date 按日期过滤，让过期文档检索不到） ----------
def retrieve_documents(query, k=5, use_bm25=True, use_vector=True, fusion="rrf", min_date=None):
    min_int = int(min_date.replace("-", "")) if min_date else None
    candidates = []
    if use_vector:
        retriever = vectorstore.as_retriever(
            search_type="mmr",
            search_kwargs={
                'k': k, 'fetch_k': k*2,
                'filter': {"date": {"$gte": min_int}} if min_int else None
            }
        )
        vector_docs = retriever.invoke(query)
        candidates.append(("vector", vector_docs))
    # BM25 检索
    if use_bm25:
        query_tokens = list(jieba.cut(query))
        bm25_scores = bm25.get_scores(query_tokens)
        top_indices = sorted(range(len(bm25_scores)), key=lambda i: bm25_scores[i], reverse=True)
        # BM25 侧同样按日期过滤，两路口径一致
        if min_int:
            top_indices = [i for i in top_indices if int(docs[i].metadata.get("date") or 0) >= min_int]
        top_indices = top_indices[:k * 2]  # 扩大候选池再融合，避免单路高排名块被 RRF 挤出
        bm25_docs = [docs[i] for i in top_indices]
        candidates.append(("bm25", bm25_docs))
    # RRF 融合
    if fusion == "rrf" and len(candidates) > 1:
        rrf_scores = {}
        for source, docs_list in candidates:
            for rank, doc in enumerate(docs_list, start=1):
                doc_key = (doc.metadata.get("source"), doc.metadata.get("chunk_index"))  # (source, chunk_index) 唯一定位，避免重复
                rrf_scores[doc_key] = rrf_scores.get(doc_key, 0) + 1 / (60 + rank)
        all_docs = {}
        for source, docs_list in candidates:
            for doc in docs_list:
                doc_key = (doc.metadata.get("source"), doc.metadata.get("chunk_index"))
                all_docs.setdefault(doc_key, doc)
        fused_docs = sorted(all_docs.values(), key=lambda d: rrf_scores.get((d.metadata.get("source"), d.metadata.get("chunk_index")), 0), reverse=True)[:k]
        return fused_docs
    # 单路返回
    if candidates:
        return candidates[0][1][:k]
    return []

# ---------- 6. 重排序（懒加载：启动时不加载大模型，避免启动失败；加载失败时跳过重排序） ----------
reranker_model = None
def rerank_documents(query, docs, top_k=3):
    global reranker_model
    if reranker_model is None:
        try:
            reranker_model = CrossEncoder("BAAI/bge-reranker-base")
        except Exception:
            return docs[:top_k]  # 加载失败时直接返回检索结果
    pairs = [[query, doc.page_content] for doc in docs]
    scores = reranker_model.predict(pairs)
    sorted_docs = sorted(zip(docs, scores), key=lambda x: x[1], reverse=True)
    return [doc for doc, score in sorted_docs[:top_k]]

# ---------- 7. 最终 RAG 回答生成（带 Redis 缓存；返回答案 + 带 chunk 索引的证据） ----------
async def rag_answer(query, k_retrieve=5, k_rerank=3, min_date=None):
    key = f"rag2:{hashlib.md5(query.encode()).hexdigest()}"
    if cached := await cache_get(key):
        return cached
    # 向量/BM25/重排序为 CPU 密集，放到线程池避免阻塞事件循环
    def _search():
        retrieved = retrieve_documents(query, k=k_retrieve, min_date=min_date)
        return rerank_documents(query, retrieved, top_k=k_rerank)
    final_docs = await asyncio.to_thread(_search)
    context = "\n\n".join([doc.page_content for doc in final_docs])
    # 证据列表：来源文件 + chunk 索引 + 片段，供回答引用与溯源
    evidence = [{
        "source": doc.metadata.get("source"),
        "chunk_index": doc.metadata.get("chunk_index"),
        "content": doc.page_content[:150]
    } for doc in final_docs]
    prompt = f"""基于以下知识库内容回答问题，如果知识库中没有相关信息，请说"不知道"。
知识库内容：
{context}

问题：{query}
回答："""
    response = await llm.ainvoke(prompt)
    result = {"answer": response.content, "evidence": evidence}
    await cache_set(key, json.dumps(result, ensure_ascii=False), 3600)
    return json.dumps(result, ensure_ascii=False)
