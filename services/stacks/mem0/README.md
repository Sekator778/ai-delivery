# stacks/mem0 — Vector memory (Qdrant only)

Local vector store for the `/memo` and `/recall` Telegram commands.
Single container, bound to `127.0.0.1` — no LAN exposure.

Embeddings are produced by **FastEmbed** in-process inside `bot/bot.py`
(ONNX Runtime, no GPU). Default model is `intfloat/multilingual-e5-large`
(1024-dim, ~2 GB ONNX cached on first call) — SOTA for multilingual
semantic search. The old Ollama + qwen2.5:14b path was retired in May
2026 — it cost ~9.5 GB of disk and 30-60 s per fact extraction.
FastEmbed runs sub-100 ms per embedding after warm-up.

## First-time bring-up

```bash
cd services/stacks/mem0
docker compose up -d
```

## Verifying

```bash
curl -s http://127.0.0.1:6333/ | jq .title    # → "qdrant - vector search engine"
curl -s http://127.0.0.1:6333/collections | jq .
```

The `meta_agent_mem` collection auto-creates on first `/memo` from
`bot.py:_ensure_collection`. Default vector size = 1024 (matches
`multilingual-e5-large`).

## Stopping

```bash
docker compose down              # keeps volumes
docker compose down -v           # nukes stored memos
```

## Disk usage

- `qdrant-data/` grows with stored memories; typically <100 MB even for
  months of use.
- FastEmbed model cache lives under `~/.cache/fastembed/` (~2 GB for the
  default `multilingual-e5-large`; ~120 MB for the smaller MiniLM fallback).

## Switching to a different embedding model

Pick any model from `fastembed.TextEmbedding.list_supported_models()`,
then set in `bot/.env`:

```bash
MEMO_EMBED_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
MEMO_EMBED_DIMS=384
```

⚠️ Existing points in the collection are tied to the old vector size.
After changing dims, drop the collection (`curl -X DELETE
http://127.0.0.1:6333/collections/meta_agent_mem`) so `_ensure_collection`
recreates it with the new size.
