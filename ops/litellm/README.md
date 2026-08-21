# LiteLLM proxy

Single Docker container that routes DeepSeek and GLM traffic from `claude -p`
through an Anthropic-passthrough endpoint at `localhost:4000/v1/messages`.
Max OAuth (anthropic backend) bypasses the proxy entirely — that connection
stays direct to `api.anthropic.com` per Anthropic's policy on OAuth.

## Why

See `STATE/DECISIONS.md` → `litellm-proxy-for-non-anthropic-backends`.

## Start / stop

```bash
cd ops/litellm
cp .env.example .env                # then edit with real keys
docker compose up -d                # foreground: drop `-d`
docker compose logs -f litellm      # tail
docker compose down                 # stop
```

## Healthcheck

```bash
curl -fsS http://localhost:4000/health/liveliness
# {"alive": true, ...}
```

## Quick smoke (after .env is populated)

```bash
curl -s http://localhost:4000/v1/messages \
  -H "x-api-key: $LITELLM_MASTER_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d '{"model":"deepseek-v4-flash","max_tokens":32,
       "messages":[{"role":"user","content":"reply with just OK"}]}'
```

Expected: an Anthropic-format `message` object whose `content[0].text` is "OK".

## Wire-up to the pipeline

NOT done in Phase 1 — see Phase 2 (separate task) which edits
`dispatcher/stage_runner_agent.py::_subagent_env` to set
`ANTHROPIC_BASE_URL=http://localhost:4000/v1` and
`ANTHROPIC_AUTH_TOKEN=$LITELLM_MASTER_KEY` for backend ∈ {deepseek, glm}.

## Trouble

| Symptom | Likely cause | Action |
|---------|--------------|--------|
| `curl: connection refused` on 4000 | container not up | `docker compose ps` |
| 401 from proxy | wrong master key in request | check `LITELLM_MASTER_KEY` matches client header |
| Always 500 from proxy | backend API key missing | check `DEEPSEEK_API_KEY` / `GLM_API_KEY` in `.env` |
| Long latency / 504 | upstream provider slow | check `docker compose logs litellm \| tail -50` for the routed request |
