# Self-healing — the ops layer

How the AI Delivery system stays up. Source — the conductor design
(`orchestrator/windmill-conductor.md`) and the architecture principle: a conductor
cannot heal itself — something is always below it.

## The layered model

```
Layer 0  Host + Docker daemon (+ systemd on a real server)   <- the OS holds this
Layer 1  Containers (Windmill stack, ai-delivery worker)      <- restart policy + autoheal
Layer 2  Conductor (Windmill ops flow)                        <- monitors dependencies, alerts
```

A failure at a layer is recovered by the layer **below** it. The conductor (layer 2)
cannot resurrect itself — that is the bootstrap limit.

## What is automatic

### 1. Crash recovery — `restart: unless-stopped`

Every service in `windmill/docker-compose.yml` has `restart: unless-stopped`. If a
container's process exits (crash), or the Docker daemon restarts, Docker brings the
container back. **Proven:** after a Docker Desktop crash on 2026-05-20 the whole stack
came back with no manual action.

Limit: this catches a process **exit**, not a process that is **hung but still running**.

### 2. Hung-service recovery — `autoheal` + healthchecks

A container can be "Up" while the app inside is unresponsive — `restart: unless-stopped`
does not catch that. So:

- Services have a Docker `healthcheck` (an HTTP/TCP probe).
- The `autoheal` container (`willfarrell/autoheal`) watches healthchecks and **restarts
  any container that goes `unhealthy`** (those labelled `autoheal=true`).

This is the literal "a service stops responding -> it is brought back up", with no human.

### 3. Dependency & application monitoring — the Windmill ops flow

A scheduled Windmill script (`f/ai_delivery/ops_healthcheck`, every N minutes):

- Checks the Windmill API, the worker registration, the external APIs (GitHub, Anthropic).
- Produces a structured health report.
- **Alerts** on anything it cannot heal.

This is the conductor monitoring its own world — the "the conductor watches the services"
part of the idea.

## What is NOT self-healable — the honest boundary

- **The Docker daemon / the host itself.** If Docker is fully down, nothing inside it
  runs — not the conductor, not autoheal. Recovery is the OS's job. On a dev machine
  (Docker Desktop) this is **manual** — a human restarts Docker. On a production Linux
  server it must be **Docker as a systemd service** with `Restart=always`: then the OS
  restarts the daemon and the container restart policies bring the stack back unattended.
- **Windmill itself fully down.** The ops flow runs inside Windmill — it cannot run if
  Windmill is down. `restart: unless-stopped` + autoheal cover the container; a deeper
  failure needs an external monitor / on-call.

"Who watches the watchmen" — the bottom is always the platform (Docker/systemd) or a
human. This cannot be designed away; it can only be pushed down to the most reliable
layer.

## Production recommendation

1. A real Linux host, not Docker Desktop.
2. Docker installed as a systemd service (`systemctl enable docker`).
3. The stack started on boot (a systemd unit, or Docker Compose with the restart
   policies) so a host reboot brings everything back unattended.
4. The ops-flow alerts wired to a real channel (Telegram via `bot/bot.py`, or email).
