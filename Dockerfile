# Runs as a Telegram long-polling worker; it does not need an HTTP port.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libgl1 libglib2.0-0 fonts-dejavu-core tesseract-ocr curl \
    && rm -rf /var/lib/apt/lists/*

# Skor grafiğinde kullanılan kalın/net font (referans görsellere en yakın ücretsiz font)
RUN mkdir -p /app/fonts && curl -sL -o /app/fonts/Anton-Regular.ttf \
    "https://raw.githubusercontent.com/google/fonts/main/ofl/anton/Anton-Regular.ttf"

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./
RUN mkdir -p /app/jobs

CMD ["python", "bot.py"]
