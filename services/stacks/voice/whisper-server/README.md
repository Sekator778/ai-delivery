# whisper-server

Local Whisper STT (speech-to-text) HTTP server. Accepts OGG audio from
Telegram voice messages, transcodes to 16 kHz mono WAV via ffmpeg, and
runs whisper.cpp to produce plain-text transcription. Serves on port 8765.

## Build

```bash
docker build -t claude-tg-whisper .
```

## Download model

```bash
mkdir -p ./models
curl -L -o ./models/ggml-medium.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin
```

The model is approximately 1.5 GB. `ggml-medium` is the smallest model that
gives reliable Russian transcription.

## Run standalone

```bash
docker run --rm -p 127.0.0.1:8765:8765 \
  -v $(pwd)/models:/models:ro \
  claude-tg-whisper
```

## Smoke test

```bash
curl -fs http://127.0.0.1:8765/health | jq
```

```bash
curl -X POST -F "file=@sample.ogg" -F "language=ru" \
  http://127.0.0.1:8765/transcribe | jq
```
