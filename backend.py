# -*- coding: utf-8 -*-
"""入口模块：FastAPI 实例、生命周期、全部 API 路由。
依赖模块：config（配置/LLM）、db（MySQL/Redis）、auth（密码/JWT）、rag（知识检索）、agent（Agent 工具与构建）。
"""
import io
import json
import pytesseract
from typing import Optional
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse
from langchain_core.messages import AIMessageChunk
from PIL import Image

import config  # 配置、LLM、OCR 路径（先导入：设置 HF_HUB_OFFLINE 等环境变量）
import db
from db import cache_get, cache_set, cache_del
from auth import hash_password, verify_password, create_access_token, get_current_user
from rag import retrieve_documents, rag_answer  # re-export：供 eval_rag.py / tests 使用
from agent import agent, init_agent, close_agent

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

# ---------- 生命周期 ----------
@asynccontextmanager
async def lifespan(app):
    await db.init_pool()   # MySQL 连接池 + 会话表
    await init_agent()     # LangGraph Agent + checkpointer（需要事件循环）
    yield
    await db.close_pool()
    await close_agent()

# ---------- FastAPI 实例 ----------
app = FastAPI(title="宠物健康管家 API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"]
)

# ---------- 基础 API 路由（注册、登录、宠物、日志） ----------
@app.post("/api/register")
async def register(user: UserRegister):
    if len(user.password) < 6:
        raise HTTPException(status_code=400, detail="密码长度至少6位")
    async with db.POOL.acquire() as conn:
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
    async with db.POOL.acquire() as conn:
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
    async with db.POOL.acquire() as conn:
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
    async with db.POOL.acquire() as conn:
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
    async with db.POOL.acquire() as conn:
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
    async with db.POOL.acquire() as conn:
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
    async with db.POOL.acquire() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute(
                "INSERT INTO conversations (user_id, title) VALUES ((SELECT id FROM users WHERE username = %s), '新对话')",
                (current_user,)
            )
            await conn.commit()
            return {"id": cursor.lastrowid, "title": "新对话"}

@app.get("/api/conversations")
async def list_conversations(current_user: str = Depends(get_current_user)):
    async with db.POOL.acquire() as conn:
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
    async with db.POOL.acquire() as conn:
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
    async with db.POOL.acquire() as conn:
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
    async with db.POOL.acquire() as conn:
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
    async with db.POOL.acquire() as conn:
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
