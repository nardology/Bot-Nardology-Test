FROM python:3.12-slim

RUN apt-get update && apt-get install -y ffmpeg curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# install deps first (better caching)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# then copy your code (bust cache: v3)
COPY . /app

# Use /health (always registered) so deploy doesn't depend on landing page. Long start-period
# lets DB/Redis/extension load finish before we're marked unhealthy (avoids deploy loop).
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=5 \
    CMD curl -f http://127.0.0.1:${PORT:-8080}/health || exit 1

CMD ["python", "bot.py"]
