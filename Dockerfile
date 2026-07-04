FROM python:3.11-slim

# faster-whisper 需要 ffmpeg 做音频解码
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制项目文件
COPY . .

# HF Spaces 要求以非 root 用户运行
RUN useradd -m -u 1000 user && chown -R user:user /app
USER user

# HF Spaces Docker 默认端口 7860
ENV PORT=7860
EXPOSE 7860

CMD ["python", "server.py"]
