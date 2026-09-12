# -*- coding: utf-8 -*-
"""Agent 层：工具定义 + LLM 编排 + LangGraph Agent 构建（含会话记忆 checkpointer）。"""
import asyncio
import json
import os
import requests
import aiosqlite
from langchain.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware

from . import db
from .db import cache_get, cache_set
from .config import llm
from .rag import rag_answer

# ---------- AI 工具定义 ----------
@tool
async def get_pet_logs(pet_id: int, hours: int = 168) -> str:
    """
        获取指定宠物最近 hours 小时的行为日志，并分析这些行为的时间间隔是否正常，如果不正常（例如：三小时前刚记录了一次排泄，三小时后又排泄了一次，或者每天睡觉时间过长/过短），请分析异常，并将分析结果返回 JSON 字符串。
        参数：
        - pet_id: 宠物ID
        - hours: 小时数，默认168(一周)
        """
    async with db.POOL.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT log_type, description, log_time
                FROM daily_logs
                WHERE pet_id = %s AND log_time >= NOW() - INTERVAL %s HOUR
                ORDER BY log_time DESC
                """,
                (pet_id, hours)
            )
            logs = await cursor.fetchall()
            for log in logs:
                log["log_time"] = log["log_time"].strftime("%Y-%m-%d %H:%M:%S")
            await conn.commit()  # 释放只读事务，避免连接池残留旧快照
            return json.dumps(logs, ensure_ascii=False)

@tool
async def get_pet_profile(pet_id: int) -> str:
    """
    获取指定宠物的基本信息档案（名字、种类、品种、年龄、体重），用于个性化回答。
    参数：
    - pet_id: 宠物ID
    """
    async with db.POOL.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT id, name, species, breed, age, weight FROM pets WHERE id = %s",
                (pet_id,)
            )
            pet = await cursor.fetchone()
            await conn.commit()  # 释放只读事务，避免连接池残留旧快照
            if not pet:
                return json.dumps({"error": "宠物不存在"}, ensure_ascii=False)
            return json.dumps(pet, ensure_ascii=False, default=str)

@tool
async def search_nearby_hospitals(city: str) -> str:
    """
    当宠物出现健康问题、生病、受伤或需要医疗帮助时，使用此工具搜索指定城市内的宠物医院。
    参数：
    - city: 城市名称，例如"深圳"或"深圳市"。请从用户消息中提取城市名，不要使用详细地址。
    返回：医院名称、地址、距离、电话等信息的 JSON 字符串。
    """
    key = f"hospital:{city}"
    if cached := await cache_get(key):
        return cached
    AMAP_KEY = os.getenv('AMAP_KEY')
    url = f"https://restapi.amap.com/v3/place/text?keywords=宠物医院&city={city}&offset=10&key={AMAP_KEY}"
    try:
        response = await asyncio.to_thread(requests.get, url)
        if response.status_code != 200:
            return json.dumps({"error": "无法获取医院数据"}, ensure_ascii=False)
        data = response.json()
        hospitals = [{
            "name": poi.get("name"),
            "address": poi.get("address"),
            "distance": poi.get("distance", "未知"),
            "phone": poi.get("tel", "未知")
        } for poi in data.get("pois", [])]
        result = json.dumps(hospitals, ensure_ascii=False)
        await cache_set(key, result, 86400)
        return result
    except Exception:
        return json.dumps({"error": "无法获取医院数据"}, ensure_ascii=False)

@tool
async def search_knowledge_base(query: str) -> str:
    """
        在宠物健康知识库中搜索相关信息。
        当用户询问疾病症状、治疗方法、喂养建议、宠物行业知识、宠物行为（睡觉吃饭排泄玩耍等等）数据时使用此工具。
        参数：
        - query: 用户的问题或关键词
        返回：基于知识库生成的回答
        """
    return await rag_answer(query)

@tool
async def web_search(query: str) -> str:
    """
    当用户需要最新资讯、实时信息、天气、新闻、或知识库中无法覆盖的内容时，使用此工具联网搜索。
    参数：
    - query: 搜索关键词
    返回：相关网页结果的 JSON 字符串
    """
    BOCHA_API_KEY = os.getenv("WEB_SEARCH_KEY", "")
    if not BOCHA_API_KEY:
        return json.dumps({"error": "未配置 WEB_SEARCH_KEY"}, ensure_ascii=False)
    try:
        response = await asyncio.to_thread(requests.post, "https://api.bochaai.com/v1/web-search",
            json={"query": query, "summary": True, "count": 5},
            headers={"Authorization": f"Bearer {BOCHA_API_KEY}"})
        if response.status_code != 200:
            return json.dumps({"error": "搜索服务异常"}, ensure_ascii=False)
        data = response.json()
        results = [{"title": r.get("name"), "url": r.get("url"),
                    "content": (r.get("summary") or r.get("snippet") or "")[:200]}
                   for r in data.get("data", {}).get("webPages", {}).get("value", [])]
        return json.dumps(results, ensure_ascii=False)
    except Exception:
        return json.dumps({"error": "搜索失败"}, ensure_ascii=False)

# ---------- 初始化 LLM 和 Agent ----------
tools = [get_pet_logs, get_pet_profile, search_nearby_hospitals, search_knowledge_base, web_search]

# 使用新版 create_agent 构建 Agent
system_prompt = """
你是一个宠物健康助手，具有以下能力：
1. 查询宠物最近的行为日志（通过 get_pet_logs 工具）。
2. 获取宠物基本信息档案（通过 get_pet_profile 工具）。
3. 搜索附近的宠物医院（通过 search_nearby_hospitals 工具）。
4. 搜索宠物健康知识库（通过 search_knowledge_base 工具），获取疾病症状、治疗方法、喂养建议等专业知识。
5. 联网搜索实时信息（通过 web_search 工具），获取最新资讯、天气、新闻、实时事件等知识库中没有的内容。

请遵循以下工作方式：
- 面对用户问题，先判断需要哪些数据，主动拆解任务并调用对应工具获取信息，不要凭空编造数据。
- 如果工具返回的结果不足以回答，应更换查询参数或调用其他工具继续尝试，反复调整直到获得足够信息，再给出最终回答。
- 当用户提到宠物可能生病、受伤、行为异常或需要医疗帮助时，应该主动关心并询问用户当前所在位置（城市或地址）。如果用户提供了位置信息，直接调用 search_nearby_hospitals 工具获取附近医院信息，并推荐给用户。例如，如果用户说"我的猫生病了，我在深圳"，你应该提取"深圳"作为 city 参数，调用工具。
- 当用户咨询具体宠物时，优先调用 get_pet_profile 获取宠物档案，结合宠物自身情况个性化回答。
- 当用户询问宠物疾病、健康问题、宠物行为（睡觉吃饭排泄玩耍等等）或需要专业建议时，优先使用 search_knowledge_base 工具获取准确信息。
- 当用户提出任何问题，如果不在你的直接知识范围内，或者涉及宠物行业相关的公司信息、财务数据、市场情况等，优先调用 search_knowledge_base 工具。不要直接拒绝用户，先尝试通过知识库查找答案。
- 当用户询问最新、实时或知识库无法覆盖的信息时，使用 web_search 工具联网搜索，不要把过时知识当答案。
- 当用户提出任何问题时，请先检查你的工具列表，看是否有工具可以回答问题。不要直接拒绝用户，除非所有工具都无法处理。

请始终保持友好、关怀的语气，并主动提供帮助。
"""

# ---------- 中间件 ----------
middleware = SummarizationMiddleware(
    model="deepseek-chat",
    trigger=("messages", 3),
    keep=("messages", 1)
)

# ---------- Agent 构建（lifespan 中调用；checkpointer 需要事件循环） ----------
agent = None
checkpointer = None

async def init_agent():
    global agent, checkpointer
    checkpointer = AsyncSqliteSaver(await aiosqlite.connect("resources/checkpoint.db"))
    await checkpointer.setup()
    agent = create_agent(llm, tools, system_prompt=system_prompt, checkpointer=checkpointer, middleware=[middleware])
    return agent

async def close_agent():
    await checkpointer.conn.close()
