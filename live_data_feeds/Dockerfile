FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ffmpeg drives the HLS video transcoder and the MP3 audio fan-out.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt && \
    pip install python-dotenv

COPY . .

EXPOSE 8181

# main.py reads PORT from .env via python-dotenv; uvicorn.run is started inside the script.
# We invoke uvicorn directly here so PORT is honoured as an integer and we don't depend on the .env file being present in the image.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8181}"]
