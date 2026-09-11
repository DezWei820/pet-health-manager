# -*- coding: utf-8 -*-
"""RAG 检索召回单测：验证知识库问题能被检索到（零 LLM 成本）"""
import backend

# 评测用例：问题 → 答案中必须出现的关键词（基于 data/ 知识库）
CASES = [
    ("猫咪绝育后需要佩戴伊丽莎白圈几天？", ["7到10", "7 到 10"]),
    ("猫的饮水量超过多少需要尽快就医？", ["100"]),
    ("成年猫每天平均睡眠多少小时？", ["14", "16"]),
]


def test_rag_retrieval_recall():
    """每个问题在前 3 个检索结果中至少命中一个含答案关键词的块"""
    for query, kws in CASES:
        docs = backend.retrieve_documents(query, k=3)
        assert any(any(kw in d.page_content for kw in kws) for d in docs), f"未检索到: {query}"
