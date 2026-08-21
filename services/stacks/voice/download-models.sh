#!/usr/bin/env bash
# download-models.sh — one-time fetch of voice models that the Docker
# containers mount at runtime.
#
# Whisper: ggml-medium model from Hugging Face (~1.5 GB, the smallest
# model that gives reliable Russian transcription).
# Silero:  pre-warmed inside its Dockerfile at build time, so nothing
# to do here for TTS.
#
# Idempotent: skips files that already exist with correct size.

set -euo pipefail

cd "$(dirname "$0")"

WHISPER_MODELS_DIR="whisper-server/models"
WHISPER_MODEL_FILE="ggml-medium.bin"
WHISPER_MODEL_URL="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium.bin"
WHISPER_EXPECTED_BYTES=1533763059   # ~1.46 GiB

mkdir -p "$WHISPER_MODELS_DIR"

if [[ -f "$WHISPER_MODELS_DIR/$WHISPER_MODEL_FILE" ]]; then
    actual=$(stat -c %s "$WHISPER_MODELS_DIR/$WHISPER_MODEL_FILE" 2>/dev/null || \
             stat -f %z "$WHISPER_MODELS_DIR/$WHISPER_MODEL_FILE")
    if [[ "$actual" == "$WHISPER_EXPECTED_BYTES" ]]; then
        echo "ok: $WHISPER_MODEL_FILE already present ($actual bytes)"
        exit 0
    else
        echo "warn: existing $WHISPER_MODEL_FILE has wrong size ($actual vs $WHISPER_EXPECTED_BYTES) — redownloading"
        rm "$WHISPER_MODELS_DIR/$WHISPER_MODEL_FILE"
    fi
fi

echo "downloading $WHISPER_MODEL_FILE (~1.5 GB) from Hugging Face..."
curl -L --fail --progress-bar -o "$WHISPER_MODELS_DIR/$WHISPER_MODEL_FILE" "$WHISPER_MODEL_URL"

actual=$(stat -c %s "$WHISPER_MODELS_DIR/$WHISPER_MODEL_FILE" 2>/dev/null || \
         stat -f %z "$WHISPER_MODELS_DIR/$WHISPER_MODEL_FILE")
echo "downloaded: $actual bytes"
[[ "$actual" == "$WHISPER_EXPECTED_BYTES" ]] || { echo "ERROR: size mismatch"; exit 1; }
echo "ok"
