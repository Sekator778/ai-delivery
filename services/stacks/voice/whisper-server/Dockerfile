FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    build-essential \
    cmake \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /whisper-build
RUN git clone --depth 1 --branch v1.7.4 https://github.com/ggerganov/whisper.cpp.git . \
    && cmake -B build \
    && cmake --build build --config Release \
    && (cp build/bin/whisper-cli /usr/local/bin/whisper-cli 2>/dev/null \
        || cp build/bin/main /usr/local/bin/whisper-cli) \
    # whisper-cli is dynamically linked against libwhisper.so.1 and libggml*.so.
    # The previous version of this Dockerfile only copied the binary, so the
    # runtime container hit "libwhisper.so.1: cannot open shared object file".
    # Copy the shared libs into the standard linker path and refresh ld cache.
    && cp build/src/libwhisper.so* /usr/local/lib/ \
    && cp build/ggml/src/libggml*.so* /usr/local/lib/ 2>/dev/null || true \
    && ldconfig \
    && rm -rf /whisper-build

RUN mkdir -p /models

WORKDIR /app
COPY requirements.txt server.py /app/
RUN pip install --no-cache-dir -r requirements.txt

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fs http://127.0.0.1:8765/health || exit 1

EXPOSE 8765

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8765"]
