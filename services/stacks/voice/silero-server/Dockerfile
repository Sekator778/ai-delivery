FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# The +cpu wheel index is amd64-only (no aarch64 wheels) — on arm64 the plain
# PyPI wheel IS cpu-only, so install per-arch to keep both platforms working.
ARG TARGETARCH
RUN if [ "$TARGETARCH" = "amd64" ]; then \
        pip install --no-cache-dir torch==2.4.1+cpu \
            --index-url https://download.pytorch.org/whl/cpu; \
    else \
        pip install --no-cache-dir torch==2.4.1; \
    fi

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

WORKDIR /app
COPY server.py /app/

RUN python -c "\
import torch; \
torch.hub.load( \
    repo_or_dir='snakers4/silero-models', \
    model='silero_tts', \
    language='ru', \
    speaker='v4_ru' \
)" && echo "Silero model cache pre-warmed"

HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -fs http://127.0.0.1:8768/health || exit 1

EXPOSE 8768

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8768"]
