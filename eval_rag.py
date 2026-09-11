# -*- coding: utf-8 -*-
"""RAG 评测脚本：recall@3（零 LLM 成本，默认跑）+ hit rate（调 LLM，--full 才跑）
用法：
  python eval_rag.py           # 只测检索召回
  python eval_rag.py --full    # 加测端到端回答命中
"""
import json, asyncio, sys
import backend

# 人工标注评测集：query → 期望答案里/命中文档里必须出现的关键词（全部基于 data/ 现有知识库）
EVAL_SET = [
    {"query": "猫咪绝育后需要佩戴伊丽莎白圈几天？", "expected": ["7到10", "7 到 10"]},
    {"query": "猫的饮水量超过多少需要尽快就医？", "expected": ["100"]},
    {"query": "成年猫每天平均睡眠多少小时？", "expected": ["14", "16"]},
    {"query": "猫的进食量减少多少且持续多久需要就医？", "expected": ["50%", "24"]},
    {"query": "成年狗每天睡眠约多少小时？", "expected": ["12", "14"]},
]

def recall_at_k(query, expected, k=3):
    """检索侧召回：前 k 个结果里是否出现含期望关键词的块（不调 LLM）"""
    docs = backend.retrieve_documents(query, k=k)
    for d in docs:
        if any(kw in d.page_content for kw in expected):
            return True
    return False

async def main():
    # 1. 检索侧 recall@3（零 LLM 成本）
    rec = sum(recall_at_k(q["query"], q["expected"]) for q in EVAL_SET)
    print(f"=== recall@3: {rec}/{len(EVAL_SET)} = {rec / len(EVAL_SET):.0%} ===")
    for q in EVAL_SET:
        print(f"[{'OK' if recall_at_k(q['query'], q['expected']) else 'XX'}] {q['query']}")

    # 2. 端到端 hit rate（调 LLM，仅 --full 时跑）
    if "--full" in sys.argv:
        hits = 0
        for q in EVAL_SET:
            result = json.loads(await backend.rag_answer(q["query"]))
            hit = any(kw in result["answer"] for kw in q["expected"])
            hits += hit
            print(f"[{'✓' if hit else '✗'}] {q['query']} -> {result['answer'][:60]}")
        print(f"=== hit rate: {hits}/{len(EVAL_SET)} = {hits / len(EVAL_SET):.0%} ===")

if __name__ == "__main__":
    asyncio.run(main())
