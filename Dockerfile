# Use slim Python base
FROM python:3.13-slim

# Set working directory
WORKDIR /app
# Install system deps
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*
# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# Copy source code
COPY . .
# Expose port
EXPOSE 3000

