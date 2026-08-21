# Runbook — operating AI Delivery

## Deployment on the Linux server (Phase 0)

1. Install: `git`, `jq`, `yq`, a container runtime (Docker/Podman), the agent harness (Claude Code or opencode), the intake bot runtime.
2. Lay out the control plane in `/opt/ai-delivery` (this directory).
3. Clone the monorepo into `/srv/monorepo`, create `/srv/worktrees`.
4. Set up the secrets in Vault (see below). Fill in `orchestrator/config.yaml` (the Jira URL, project_key, the telegram chat_id, allowlist, approvers).
5. Install the systemd units (below), `systemctl enable --now ai-intake-bot ai-orchestrator`.
6. Drive 1-2 tasks manually with `autonomy_level: pr`, make sure the stages pass.

## Secrets (Vault — never in files/git/logs)

`ANTHROPIC_API_KEY`, `ZAI_API_KEY`, `MOONSHOT_API_KEY`, `GOOGLE_VERTEX_CREDENTIALS`,
`JIRA_API_TOKEN`, `TELEGRAM_BOT_TOKEN`, `STAGING_DEPLOY_KEY`, `PROD_DEPLOY_KEY`.
The prod key is issued only to the `prod-deploy` step, not to the pipeline agents.

## systemd units

`/etc/systemd/system/ai-orchestrator.service`:
```ini
[Unit]
Description=AI Delivery orchestrator
After=network-online.target

[Service]
Type=simple
User=ai-delivery
Environment=AI_DELIVERY_HOME=/opt/ai-delivery
ExecStart=/opt/ai-delivery/orchestrator/orchestrator.sh
Restart=always
RestartSec=10
StandardOutput=append:/var/log/ai-delivery/orchestrator.log
StandardError=append:/var/log/ai-delivery/orchestrator.log

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/ai-intake-bot.service` — similarly, `ExecStart` = launching the bot.

## Daily operations

- **On-call.** At least once a day, check `tasks/awaiting-input/`, `tasks/awaiting-approval/`, `tasks/failed/`.
- **Prod approval.** The on-call person looks at staging + `gate-report.md` + the diff → Approve/Reject in the bot.
- **Triage of `failed`.** Read `worklog.md` and `gate-report.md`; either fix it and revive the task, or close it with a conclusion in `memory-bank/`.

## Monitoring

- Logs: `/var/log/ai-delivery/*.log`. Harness sessions: `harness.session_dir`.
- A `tmux` session on the server — to visually see the workers.
- The `/status` bot command — a queue summary.

## Alerts (escalate to a human immediately)

- The orchestrator is restarting in a loop → check the logs, stop the service.
- A task exceeded `cost_cap_usd` → it is paused, investigate.
- A secret found in the code / a request for an irreversible action → the task moves to `failed`, manual triage.
- Model degradation (aistupidlevel.info) → temporarily switch the provider in `config.yaml`.

## Emergency stop

`systemctl stop ai-orchestrator` — no new stages start. The agents already running will finish their stage. Production is not affected by this (it is behind the human gate anyway).

## Recurring

- Weekly: a skills audit (sprawl), a FinOps summary of cost by phase, an update of `memory-bank/`.
- Calibration of the gate thresholds based on false-positive statistics.
