# -*- coding: utf-8 -*-
"""数据库层：MySQL 连接池、Redis 缓存（含熔断降级）、会话表初始化。"""
import os
import time
import aiomysql
import redis.asyncio as aioredis
from .config import DB_HOST, DB_USER, DB_PASSWORD, DB_NAME

# ---------- Redis 缓存 ----------
redis_client = aioredis.from_url(
    os.getenv("REDIS_URL", "redis://localhost:6379/0"), decode_responses=True,
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
os.makedirs("resources", exist_ok=True)  # checkpointer 数据库所在目录

# ---------- MySQL 连接池 ----------
POOL = None

async def init_pool():
    """lifespan 启动时创建连接池并确保会话表存在（需要事件循环）"""
    global POOL
    POOL = await aiomysql.create_pool(
        host=DB_HOST, user=DB_USER, password=DB_PASSWORD, db=DB_NAME,
        cursorclass=aiomysql.DictCursor, minsize=1, maxsize=10,
        pool_recycle=3600  # 连接超过1小时自动重建，避免MySQL空闲断开后复用死连接
    )
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

async def close_pool():
    """lifespan 退出时关闭连接池"""
    POOL.close()
    await POOL.wait_closed()
