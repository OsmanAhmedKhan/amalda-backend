# Use Python 3.12 slim for stable Protobuf and ML library compilation
FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=10000

# Memory optimization for 512MB free tier limits
ENV MALLOC_ARENA_MAX=2
ENV MAX_WORKERS=1
ENV WEB_CONCURRENCY=1
ENV OMP_NUM_THREADS=1

# Install system dependencies (Removed Tesseract & libmagic1 to prevent OOM)
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    libpq-dev \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire backend directory into the container
COPY . .

# Ensure logging directory exists
RUN mkdir -p /app/logs && chmod -R 777 /app/logs

# Expose the API port
EXPOSE 10000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f -I http://localhost:10000/health || exit 1

# Start the application using Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]