# -*- coding: utf-8 -*-
"""全局配置：环境变量、LLM、OCR 路径。所有模块依赖的公共底层。"""
import os

# 嵌入/重排序模型已本地缓存，必须在导入 huggingface_hub 前设置，否则会联网检查导致启动卡死
os.environ.setdefault("HF_HUB_OFFLINE", "1")

import pytesseract
from langchain_openai import ChatOpenAI

# ---------- 配置 ----------
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = "root"
DB_PASSWORD = os.getenv("DB_PASSWORD", "123456")
DB_NAME = "pet"

SECRET_KEY = os.getenv("SECRET_KEY", "your-secret-key-change-in-production")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24

# DeepSeek API Key（从环境变量读取）
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "your-deepseek-api-key")

# OCR 引擎路径（Windows 默认路径；Docker 内通过 TESSERACT_CMD 覆盖）
pytesseract.pytesseract.tesseract_cmd = os.getenv("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")

# ---------- LLM ----------
llm = ChatOpenAI(
    model="deepseek-chat",
    temperature=0.3,
    api_key=os.environ.get('DEEPSEEK_API_KEY'),
    base_url="https://api.deepseek.com"
)
