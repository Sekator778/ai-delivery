#!/usr/bin/env bash
# check-recovery.sh — post-reboot sanity check for ai-delivery.
#
# Pre-reboot snapshot (recorded 2026-05-24 ~14:55 CEST):
#   task:       tg-20260524-124913-11ec
#   stage:      architect (BA done @ $0.48, 01-ba.md + partial 02-architecture.md on disk)
#   runner pid: 93601 (dies on reboot)
#   services:   claude-tg-bot, task-dispatcher, watcher — all active
#
# Expected after reboot:
#   1. systemd auto-starts 3 services (via Phase 2 install)
#   2. watcher.py scans tasks/active/ at boot
#   3. watcher finds tg-20260524-124913-11ec with stale .runner.pid (pid 93601 dead)
#   4. watcher respawns stage_runner_agent.py
#   5. stage_runner sees 01-ba.md exists → skips BA stage (artifact resume)
#   6. stage_runner re-runs Architect (02 may need rerun if marker not reached)
#   7. Pipeline continues toward Developer → Tester → Security → Reviewer

set -u
PASS=$'\033[32m✓\033[0m'
FAIL=$'\033[31m✗\033[0m'
WARN=$'\033[33m⚠\033[0m'

echo "==== ai-delivery — post-reboot recovery check ===="
echo ""

# 1. Services
echo "── systemd services ──"
for svc in claude-tg-bot task-dispatcher watcher ai-delivery-windmill; do
  state=$(systemctl is-active "$svc" 2>&1)
  case "$state" in
    active)   echo "  $PASS $svc: active" ;;
    *)        echo "  $FAIL $svc: $state" ;;
  esac
done
echo ""

# 2. Bot live
echo "── bot process ──"
bot_pid=$(systemctl show -p MainPID --value claude-tg-bot 2>/dev/null)
if [ -n "$bot_pid" ] && [ "$bot_pid" != "0" ]; then
  echo "  $PASS bot.py PID=$bot_pid, cwd=$(readlink /proc/$bot_pid/cwd 2>/dev/null)"
else
  echo "  $FAIL bot.py not running"
fi
echo ""

# 3. Containers
echo "── windmill stack ──"
container_count=$(docker ps --filter "name=windmill" --format '{{.Names}}' 2>/dev/null | wc -l)
echo "  $PASS $container_count windmill containers up"
echo ""

# 4. Task recovery
TID="tg-20260524-124913-11ec"
echo "── task $TID ──"
loc=""
for d in active awaiting-input awaiting-approval done failed; do
  if [ -d "tasks/$d/$TID" ]; then loc=$d; break; fi
done
if [ -z "$loc" ]; then
  echo "  $FAIL task disappeared from all queues!"
  exit 1
fi
echo "  $PASS task in tasks/$loc/"
echo "  current stage:   $(jq -r .stage tasks/$loc/$TID/state.json 2>/dev/null)"
echo "  history events:  $(jq -r '.history|length' tasks/$loc/$TID/state.json 2>/dev/null) (was 4 pre-reboot)"
echo "  artifacts:       $(ls tasks/$loc/$TID/*.md 2>/dev/null | xargs -I{} basename {} | tr '\n' ' ')"
echo ""

# 5. Runner respawn check
echo "── stage_runner ──"
pid_file="tasks/$loc/$TID/.runner.pid"
if [ -f "$pid_file" ]; then
  new_pid=$(cat "$pid_file")
  if pgrep -F "$pid_file" >/dev/null 2>&1; then
    echo "  $PASS new stage_runner PID=$new_pid alive (old was 93601)"
    if [ "$new_pid" = "93601" ]; then
      echo "  $WARN same PID as pre-reboot — did reboot actually happen?"
    fi
  else
    echo "  $FAIL .runner.pid contains $new_pid but process not alive"
  fi
else
  echo "  $WARN no .runner.pid file — task may have completed or been moved"
fi
echo ""

# 6. Watcher journal evidence
echo "── watcher journal (last 5 lines from this boot) ──"
journalctl -u watcher.service -b 0 --no-pager 2>/dev/null | tail -5 | sed 's/^/  /'
echo ""

# 7. Worklog tail
echo "── worklog last 10 lines ──"
tail -10 "tasks/$loc/$TID/worklog.md" 2>/dev/null | sed 's/^/  /'
echo ""

echo "==== summary ===="
echo "Reboot recovery passes if:"
echo "  - all services 'active'"
echo "  - task still in active/ (or moved forward to awaiting-*/done/)"
echo "  - .runner.pid has a NEW pid that's alive (not 93601)"
echo "  - worklog has new 'stage X started/completed' lines after the reboot timestamp"
echo "  - watcher journal mentions 'spawned runner pid=... for $TID' from current boot"
