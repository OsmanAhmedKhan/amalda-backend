# Use Python 3.12 slim for stable Protobuf and ML library compilation
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=10000

# Memory optimization for 512MB free tier limits
ENV MALLOC_ARENA_MAX=2
ENV MAX_WORKERS=1
ENV WEB_CONCURRENCY=1
ENV OMP_NUM_THREADS=1

# 🔥 RESTORED OCR: Re-added tesseract-ocr
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    libpq-dev \
    gcc \
    tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN mkdir -p /app/logs && chmod -R 777 /app/logs

EXPOSE 10000

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f -I http://localhost:10000/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]