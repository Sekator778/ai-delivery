# Windmill integration

The Phase-5 scheduler (`/schedule`) talks to a self-hosted
[Windmill](https://www.windmill.dev/) instance over its API. This directory holds
only what the framework itself needs:

- `docker-compose.yml` + `Caddyfile` + `.env` — the Windmill stack (server,
  workers, Postgres, Caddy).
- `worker-ai-delivery/` — a custom worker image with `../tasks/inbox` bind-mounted,
  so the `pipeline_trigger` flow can drop a `spec.json` the dispatcher picks up.
- `flows/` — the pipeline flows (`pipeline-trigger`, etc.) to deploy into the
  `ai-delivery` workspace.
- `reference/` — upstream compose/env reference.

## The `wmill` CLI is NOT vendored

`wmill` is Windmill's own command-line tool. Install it from npm when you need to
push flows or manage the workspace:

```bash
npm install -g windmill-cli          # provides the `wmill` command

# point it at your local instance, then push the pipeline flow:
wmill workspace add ai-delivery ai-delivery http://localhost/ --token <TOKEN>
wmill flow push flows/pipeline-trigger.flow f/ai_delivery/pipeline_trigger
```

> Earlier revisions vendored the full `wmill` CLI source (and its skill docs) under
> `windmill/wmill/`. That was removed in v0.8 — it is an external tool, not part of
> this framework, and it accounted for ~30% of the repo size.
