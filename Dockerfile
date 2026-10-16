# Use the official Python 3.13 slim image for a small, secure footprint
FROM python:3.13-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=10000

# Memory optimization for 512MB free tier limits
ENV MALLOC_ARENA_MAX=2
ENV MAX_WORKERS=1
ENV WEB_CONCURRENCY=1
ENV OMP_NUM_THREADS=1

# Install system dependencies required for PyMuPDF, Tesseract OCR, PostgreSQL, MIME sniffing, and networking
RUN apt-get update && apt-get install -y \
    build-essential \
    tesseract-ocr \
    curl \
    libpq-dev \
    gcc \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the entire backend directory into the container
COPY . .

# Ensure data directories exist
RUN mkdir -p /app/data /app/logs

# Expose the API port
EXPOSE 10000

# Health check to ensure the container restarts if the API crashes
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:10000/health || exit 1

# Start the application using Uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]