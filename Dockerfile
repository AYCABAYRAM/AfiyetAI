# AfiyetAI Production Dockerfile - SIFIRDAN
FROM python:3.11-slim

# Metadata
LABEL maintainer="AfiyetAI Team"
LABEL version="1.0"
LABEL description="AfiyetAI Receipt Scanner & Recipe Recommender"

# Environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Build tools
    build-essential \
    gcc \
    g++ \
    pkg-config \
    # PostgreSQL
    libpq-dev \
    libpq5 \
    # Tesseract OCR
    tesseract-ocr \
    tesseract-ocr-tur \
    tesseract-ocr-eng \
    # OpenCV dependencies
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    libgomp1 \
    libgl1 \
    libfontconfig1 \
    libice6 \
    libjpeg62-turbo \
    libpng16-16 \
    libtiff6 \
    libopenjp2-7 \
    libavcodec-dev \
    libavformat-dev \
    libswscale-dev \
    libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first (for caching)
COPY requirements.txt .

# Install Python packages
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories
RUN mkdir -p /app/uploads /app/data /app/static /app/templates

# Create non-root user
RUN groupadd -r afiyetai && \
    useradd -r -g afiyetai afiyetai && \
    chown -R afiyetai:afiyetai /app

# Switch to non-root user
USER afiyetai

# Expose port
EXPOSE 5001

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:5001/', timeout=5)" || exit 1

# Start command
CMD ["gunicorn", "--bind", "0.0.0.0:5001", "--workers", "4", "--threads", "2", "--timeout", "120", "--worker-class", "gthread", "--access-logfile", "-", "--error-logfile", "-", "--log-level", "info", "app:app"]
