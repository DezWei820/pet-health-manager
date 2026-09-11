import os

# 嵌入/重排序模型已本地缓存，必须在导入 huggingface_hub 前设置，否则会联网检查导致启动卡死
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
import asyncio
import time
import hashlib
import io
import json
import re
from collections import defaultdict
import requests
import aiosqlite
import aiomysql
import redis.asyncio as aioredis
from jose import JWTError, jwt
from langchain_openai import ChatOpenAI
from langchain.tools import tool
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langchain.agents import create_agent
from langchain.agents.middleware import SummarizationMiddleware
from sse_starlette.sse import EventSourceResponse
from langchain_community.document_loaders import TextLoader, PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_huggingface import HuggingFaceEmbeddings
from sentence_transformers import CrossEncoder
from langchain_core.documents import Document
from langchain_core.messages import AIMessageChunk
from rank_bm25 import BM25Okapi
from PIL import Image
import pytesseract
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
import jieba

# ---------- 配置 ----------
DB_HOST = "localhost"
DB_USER = "root"
DB_PASSWORD = os.getenv("DB_PASSWORD", "123456")
DB_NAME = "pet"

SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

# DeepSeek API Key（从环境变量读取，也可以直接写在这里）
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "your-deepseek-api-key")

# ---------- Redis 缓存 ----------
redis_client = aioredis.from_url(
    "redis://localhost:6379/0", decode_responses=True,
    socket_connect_timeout=1, socket_timeout=1, protocol=2
)
_redis_ok = True  # Redis 熔断：故障后每 10s 放行一次探活，恢复后自动回归缓存
_redis_retry_at = 0.0

def _redis_ready():
    global _redis_ok, _redis_retry_at
    return _redis_ok or time.time() >= _redis_retry_at

async def cache_get(key):
    global _redis_ok, _redis_retry_at
    if not _redis_ready():
        return None
    try:
        val = await redis_client.get(key)
        _redis_ok = True
        return val
    except Exception:
        _redis_ok = False
        _redis_retry_at = time.time() + 10
        return None

async def cache_set(key, value, ex):
    global _redis_ok, _redis_retry_at
    if not _redis_ready():
        return
    try:
        await redis_client.set(key, value, ex=ex)
        _redis_ok = True
    except Exception:
        _redis_ok = False
        _redis_retry_at = time.time() + 10

async def cache_del(key):
    global _redis_ok, _redis_retry_at
    if not _redis_ready():
        return
    try:
        await redis_client.delete(key)
        _redis_ok = True
    except Exception:
        _redis_ok = False
        _redis_retry_at = time.time() + 10

# ---------- 连接 Sqlite ----------
os.makedirs("resources", exist_ok=True)
checkpointer = None  # 异步 checkpointer 在 lifespan 中创建（需要事件循环）

# ---------- 中间件 ----------
middleware = SummarizationMiddleware(
    model="deepseek-chat",
    trigger=("messages", 3),
    keep=("messages", 1)
)

# ---------- MySQL 连接池 ----------
POOL = None

@asynccontextmanager
async def lifespan(app):
    global POOL, checkpointer, agent
    POOL = await aiomysql.create_pool(
        host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME,
        cursorclass=aiomysql.DictCursor, minsize=1, maxsize=10,
        pool_recycle=3600  # 连接超过1小时自动重建，避免MySQL空闲断开后复用死连接
    )
    # 确保会话相关表存在
    async with POOL.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    user_id INT NOT NULL,
                    title VARCHAR(100) DEFAULT '新对话',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                    INDEX idx_conv_user (user_id)
                )""")
            await cursor.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    conversation_id INT NOT NULL,
                    role VARCHAR(20) NOT NULL,
                    content TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_msg_conv (conversation_id)
                )""")
            await conn.commit()
    checkpointer = AsyncSqliteSaver(await aiosqlite.connect("resources/checkpoint.db"))
    await checkpointer.setup()
    agent = create_agent(llm, tools, system_prompt=system_prompt, checkpointer=checkpointer, middleware=[middleware])
    yield
    POOL.close()
    await POOL.wait_closed()
    await checkpointer.conn.close()

# ---------- FastAPI 实例 ----------
app = FastAPI(title="宠物健康管家 API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# ---------- 密码哈希 ----------
def hash_password(password: str) -> str:
    salt = os.urandom(16).hex()
    return salt + ":" + hashlib.sha256((salt + password).encode()).hexdigest()

def verify_password(password: str, hashed: str) -> bool:
    try:
        salt, hash_value = hashed.split(":")
        return hash_value == hashlib.sha256((salt + password).encode()).hexdigest()
    except:
        return False

# ---------- JWT ----------
def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(authorization: str = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="未登录")
    token = authorization.split(" ")[1]
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=401, detail="无效的token")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="无效的token")

# ---------- Pydantic 模型 ----------
class UserRegister(BaseModel):
    username: str
    password: str

class UserLogin(BaseModel):
    username: str
    password: str

class PetCreate(BaseModel):
    name: str
    species: str
    age: float = 0.0
    breed: Optional[str] = None
    weight: float = 0.0

class LogCreate(BaseModel):
    pet_id: int
    log_type: Optional[str] = None
    description: str
    log_time: datetime

class ChatRequest(BaseModel):
    message: str
    pet_id: Optional[int] = None
    conversation_id: Optional[int] = None

# ---------- 基础 API 路由（注册、登录、宠物、日志） ----------
@app.post("/api/register")
async def register(user: UserRegister):
    if len(user.password) < 6:
        raise HTTPException(status_code=400, detail="密码长度至少6位")
    async with POOL.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT id FROM users WHERE username = %s", (user.username,))
            if await cursor.fetchone():
                raise HTTPException(status_code=400, detail="用户名已被注册")
            hashed = hash_password(user.password)
            await cursor.execute(
                "INSERT INTO users (username, password_hash) VALUES (%s, %s)",
                (user.username, hashed)
            )
            await conn.commit()
            return {"message": "注册成功"}

@app.post("/api/login")
async def login(user: UserLogin):
    async with POOL.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT password_hash FROM users WHERE username = %s", (user.username,))
            result = await cursor.fetchone()
            if not result or not verify_password(user.password, result["password_hash"]):
                raise HTTPException(status_code=401, detail="用户名或密码错误")
            token = create_access_token({"sub": user.username})
            return {"message": "登陆成功", "access_token": token, "token_type": "bearer"}

@app.get("/api/pets")
async def get_pets(current_user: str = Depends(get_current_user)):
    cache_key = f"pets:{current_user}"
    if cached := await cache_get(cache_key):
        return json.loads(cached)
    async with POOL.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT id, name, species, breed, age, weight, created_at
                FROM pets
                WHERE user_id = (SELECT id FROM users WHERE username = %s)
                ORDER BY created_at DESC
                """,
                (current_user,)
            )
            pets = await cursor.fetchall()
            for pet in pets:
                pet["created_at"] = pet["created_at"].strftime("%Y-%m-%d %H:%M:%S")
                pet["weight"] = float(pet["weight"])  # Decimal 无法 JSON 序列化，转 float
            await conn.commit()  # 释放只读事务，避免连接池复用旧快照
    await cache_set(cache_key, json.dumps(pets, ensure_ascii=False), 60)
    return pets

@app.post("/api/pets", status_code=201)
async def create_pet(pet: PetCreate, current_user: str = Depends(get_current_user)):
    if not pet.name.strip():
        raise HTTPException(status_code=400, detail="宠物名字不能为空")
    async with POOL.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT id FROM users WHERE username = %s", (current_user,))
            user_result = await cursor.fetchone()
            if not user_result:
                raise HTTPException(status_code=404, detail="用户不存在")
            await cursor.execute(
                "INSERT INTO pets (user_id, name, species, age, breed, weight) VALUES (%s, %s, %s, %s, %s, %s)",
                (user_result["id"], pet.name, pet.species, pet.age, pet.breed, pet.weight)
            )
            await conn.commit()
    await cache_del(f"pets:{current_user}")
    return {"message": f"已添加宠物 {pet.name}"}

@app.delete("/api/pets/{pet_id}")
async def delete_pet(pet_id: int, current_user: str = Depends(get_current_user)):
    async with POOL.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "DELETE FROM pets WHERE id = %s AND user_id = (SELECT id FROM users WHERE username = %s)",
                (pet_id, current_user)
            )
            await conn.commit()
            if cursor.rowcount == 0:
                raise HTTPException(status_code=404, detail="宠物不存在或无权删除")
    await cache_del(f"pets:{current_user}")
    return {"message": "删除成功"}

@app.post("/api/logs", status_code=201)
async def create_log(log: LogCreate, current_user: str = Depends(get_current_user)):
    async with POOL.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT id FROM pets WHERE id = %s AND user_id = (SELECT id FROM users WHERE username = %s)",
                (log.pet_id, current_user)
            )
            if not await cursor.fetchone():
                raise HTTPException(status_code=404, detail="宠物不存在")
            await cursor.execute(
                "INSERT INTO daily_logs (pet_id, log_type, description, log_time) VALUES (%s, %s, %s, %s)",
                (log.pet_id, log.log_type, log.description, log.log_time)
            )
            await conn.commit()
            return {"message": "记录已保存"}

# ---------- OCR 图片识别 ----------
@app.post("/api/ocr")
async def ocr_image(file: UploadFile, current_user: str = Depends(get_current_user)):
    try:
        image = Image.open(io.BytesIO(await file.read()))
        return {"text": pytesseract.image_to_string(image, lang="chi_sim+eng", config="--tessdata-dir resources/tessdata").strip()}
    except Exception as e:
        return {"text": "", "error": f"OCR 识别失败: {e}"}

# ---------- 会话管理路由 ----------
@app.post("/api/conversations", status_code=201)
async def create_conversation(current_user: str = Depends(get_current_user)):
    async with POOL.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "INSERT INTO conversations (user_id, title) VALUES ((SELECT id FROM users WHERE username = %s), '新对话')",
                (current_user,)
            )
            await conn.commit()
            return {"id": cursor.lastrowid, "title": "新对话"}

@app.get("/api/conversations")
async def list_conversations(current_user: str = Depends(get_current_user)):
    async with POOL.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                """
                SELECT c.id, c.title, c.updated_at
                FROM conversations c
                WHERE c.user_id = (SELECT id FROM users WHERE username = %s)
                ORDER BY c.updated_at DESC
                """,
                (current_user,)
            )
            convs = await cursor.fetchall()
            for c in convs:
                c["updated_at"] = c["updated_at"].strftime("%Y-%m-%d %H:%M:%S")
            await conn.commit()  # 释放只读事务，避免连接池复用旧快照
            return convs

@app.get("/api/conversations/{conv_id}/messages")
async def get_conversation_messages(conv_id: int, current_user: str = Depends(get_current_user)):
    async with POOL.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT id FROM conversations WHERE id = %s AND user_id = (SELECT id FROM users WHERE username = %s)",
                (conv_id, current_user)
            )
            if not await cursor.fetchone():
                raise HTTPException(status_code=404, detail="会话不存在")
            await cursor.execute(
                "SELECT role, content FROM messages WHERE conversation_id = %s ORDER BY id",
                (conv_id,)
            )
            rows = await cursor.fetchall()
            await conn.commit()  # 释放只读事务，避免连接池复用旧快照
            return rows

@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: int, current_user: str = Depends(get_current_user)):
    async with POOL.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT id FROM conversations WHERE id = %s AND user_id = (SELECT id FROM users WHERE username = %s)",
                (conv_id, current_user)
            )
            if not await cursor.fetchone():
                raise HTTPException(status_code=404, detail="会话不存在")
            await cursor.execute("DELETE FROM messages WHERE conversation_id = %s", (conv_id,))
            await cursor.execute("DELETE FROM conversations WHERE id = %s", (conv_id,))
            await conn.commit()
    return {"message": "会话已删除"}

async def save_messages(conv_id: int, current_user: str, user_msg: str, ai_reply: str):
    async with POOL.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "update conversations set title = if(title = '新对话', left(%s, 20), title), updated_at = CURRENT_TIMESTAMP where id = %s and user_id = (select id from users where username = %s)",
                (user_msg, conv_id, current_user)
            )
            await cursor.execute(
                "insert into messages (conversation_id, role, content) values (%s, 'user', %s), (%s, 'assistant', %s)",
                (conv_id, user_msg, conv_id, ai_reply)
            )
            await conn.commit()

# ---------- AI 聊天路由 ----------
async def _pet_context(current_user):
    """注入宠物 ID 映射，让 LLM 能把宠物名对应到 pet_id 调用工具"""
    async with POOL.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "SELECT id, name FROM pets WHERE user_id = (SELECT id FROM users WHERE username = %s)",
                (current_user,))
            pets = await cursor.fetchall()
    return " 你的宠物：" + "、".join(f"{p['name']}(ID={p['id']})" for p in pets) + "。" if pets else ""

@app.post("/api/ai/chat")
async def ai_chat(request: ChatRequest, current_user: str = Depends(get_current_user)):
    #设定thread_id,设定会话标识
    config = {"configurable": {"thread_id": current_user, "recursion_limit": 50}}
    # 构造包含上下文的用户消息
    context = f"当前用户：{current_user}。" + await _pet_context(current_user)
    if request.pet_id:
        context += f" 用户正在咨询的宠物ID：{request.pet_id}。"
    user_message = context + "\n用户问题：" + request.message

    try:
        # create_agent 返回的 agent 可以直接 invoke，传入 messages 列表
        result = await agent.ainvoke({"messages": [{"role": "user", "content": user_message}]}, config)
        # 从结果中提取最后一条 AI 消息的内容
        reply = result["messages"][-1].content
        return {"reply": reply}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI服务错误: {str(e)}")

# ---------- AI 流式聊天路由（SSE） ----------
@app.post("/api/ai/chat/stream")
async def ai_chat_stream(request: ChatRequest, current_user: str = Depends(get_current_user)):
    # 每个会话独立 thread_id，agent 按会话隔离上下文
    thread_id = request.conversation_id or current_user
    config = {"configurable": {"thread_id": thread_id, "recursion_limit": 50}}
    context = f"当前用户：{current_user}。" + await _pet_context(current_user)
    if request.pet_id:
        context += f" 用户正在咨询的宠物ID：{request.pet_id}。"
    user_message = context + "\n用户问题：" + request.message

    async def event_gen():
        reply = ""
        try:
            # stream_mode="messages" 逐 token 返回 AI 消息增量，实现真正的流式输出
            # 仅推送 model 节点的增量，过滤 SummarizationMiddleware 生成的摘要 token
            async for chunk, meta in agent.astream(
                {"messages": [{"role": "user", "content": user_message}]}, config, stream_mode="messages"):
                if meta.get("langgraph_node") == "model" and isinstance(chunk, AIMessageChunk) and chunk.content:
                    content = chunk.content
                    if isinstance(content, list):  # 兼容多内容块格式
                        content = "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
                    reply += content
                    # SSE 中 \n 是帧分隔符，需转义为字面 \n 再发送，前端还原，否则换行会丢失
                    yield {"event": "token", "data": content.replace("\n", "\\n")}
        except Exception as e:
            yield {"event": "error", "data": str(e)}
        # 流式结束后把对话落库（仅当属于某个会话）
        if request.conversation_id:
            try:
                await save_messages(request.conversation_id, current_user, request.message, reply)
            except Exception:
                pass  # 落库失败不影响已输出的内容
        yield {"event": "done", "data": ""}

    return EventSourceResponse(event_gen())

# ---------- AI 工具定义 ----------
@tool
async def get_pet_logs(pet_id: int, hours: int = 168) -> str:
    """
        获取指定宠物最近 hours 小时的行为日志，并分析这些行为的时间间隔是否正常，如果不正常（例如：三小时前刚记录了一次排泄，三小时后又排泄了一次，或者每天睡觉时间过长/过短），请分析异常，并将分析结果返回 JSON 字符串。
        参数：
        - pet_id: 宠物ID
        - hours: 小时数，默认168(一周)
        """
    async with POOL.acquire() as conn:
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
    async with POOL.acquire() as conn:
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
    - city: 城市名称，例如“深圳”或“深圳市”。请从用户消息中提取城市名，不要使用详细地址。
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
llm = ChatOpenAI(
    model="deepseek-chat",
    temperature=0.3,
    api_key=os.environ.get('DEEPSEEK_API_KEY'),
    base_url="https://api.deepseek.com"
)

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
- 当用户提到宠物可能生病、受伤、行为异常或需要医疗帮助时，应该主动关心并询问用户当前所在位置（城市或地址）。如果用户提供了位置信息，直接调用 search_nearby_hospitals 工具获取附近医院信息，并推荐给用户。例如，如果用户说“我的猫生病了，我在深圳”，你应该提取“深圳”作为 city 参数，调用工具。
- 当用户咨询具体宠物时，优先调用 get_pet_profile 获取宠物档案，结合宠物自身情况个性化回答。
- 当用户询问宠物疾病、健康问题、宠物行为（睡觉吃饭排泄玩耍等等）或需要专业建议时，优先使用 search_knowledge_base 工具获取准确信息。
- 当用户提出任何问题，如果不在你的直接知识范围内，或者涉及宠物行业相关的公司信息、财务数据、市场情况等，优先调用 search_knowledge_base 工具。不要直接拒绝用户，先尝试通过知识库查找答案。
- 当用户询问最新、实时或知识库无法覆盖的信息时，使用 web_search 工具联网搜索，不要把过时知识当答案。
- 当用户提出任何问题时，请先检查你的工具列表，看是否有工具可以回答问题。不要直接拒绝用户，除非所有工具都无法处理。

请始终保持友好、关怀的语气，并主动提供帮助。
"""
# agent 在 lifespan 启动时创建（需要事件循环中的 AsyncSqliteSaver）

# ---------- RAG模块 ----------
# 1. 加载文档（支持 txt, pdf, docx, 图片 OCR；所有类型统一数据清洗）
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

# 2. 切分文档
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

# 3. 构建 BM25 索引（必须在 docs 生成之后）
tokenized_corpus = [list(jieba.cut(doc.page_content)) for doc in docs]
bm25 = BM25Okapi(tokenized_corpus)

# 4. 文本向量化与向量数据库
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

# 5. 多路召回函数（向量 + BM25 + RRF 融合；min_date 按日期过滤，让过期文档检索不到）
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
        top_indices = top_indices[:k]
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

# 6. 重排序（懒加载：启动时不加载大模型，避免启动失败；加载失败时跳过重排序）
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

# 7. 最终 RAG 回答生成（带 Redis 缓存；返回答案 + 带 chunk 索引的证据）
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
    prompt = f"""基于以下知识库内容回答问题，如果知识库中没有相关信息，请说“不知道”。
知识库内容：
{context}

问题：{query}
回答："""
    response = await llm.ainvoke(prompt)
    result = {"answer": response.content, "evidence": evidence}
    await cache_set(key, json.dumps(result, ensure_ascii=False), 3600)
    return json.dumps(result, ensure_ascii=False)
