# Use a lightweight official Python image
FROM python:3.10-slim

# Install system dependencies including native FFmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set our application working space
WORKDIR /app

# Copy and install Python requirements first for layer caching optimization
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application backend code
COPY . .

# Expose the standard routing network port
EXPOSE 10000

# Start Uvicorn bound to Render's global variable port
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}"]