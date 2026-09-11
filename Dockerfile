# 后端镜像：FastAPI + LangGraph Agent + RAG + OCR
FROM python:3.13-slim
WORKDIR /app

# OCR 系统依赖（Tesseract + 中文语言包）
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-chi-sim && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# 向量/重排序模型从挂载的宿主机缓存加载（不联网）
ENV HF_HUB_OFFLINE=1
EXPOSE 8000
CMD ["uvicorn", "backend:app", "--host", "0.0.0.0", "--port", "8000"]
