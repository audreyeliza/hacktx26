#!/usr/bin/env bash
# Copy the project from this Mac to the Raspberry Pi (code only; the Pi keeps its own .venv and data/).
# Run ON THE MAC, from the repo root:
#     ./deploy/push_to_pi.sh                 # sync code
#     ./deploy/push_to_pi.sh --env           # also copy .env (Gemini key) with mode 600
#     ./deploy/push_to_pi.sh --setup         # sync, then run deploy/pi_setup.sh on the Pi
#     ./deploy/push_to_pi.sh --restart       # sync, then restart the service
# Flags can be combined. Target: audreybolyard@audreybolyard.local:~/hacktx
set -euo pipefail

HOST="${PI_HOST:-audreybolyard.local}"
USER_AT="audreybolyard@$HOST"
DEST="~/hacktx"
COPY_ENV=0; SETUP=0; RESTART=0
for arg in "$@"; do
    case "$arg" in
        --env) COPY_ENV=1 ;;
        --setup) SETUP=1 ;;
        --restart) RESTART=1 ;;
        *) echo "unknown option $arg" >&2; exit 2 ;;
    esac
done

cd "$(dirname "$0")/.."
[[ -f AGENTS.md && -d server ]] || { echo "run from the BreathWatch repo" >&2; exit 1; }

echo "==> syncing code to $USER_AT:$DEST"
ssh "$USER_AT" "mkdir -p $DEST"
# --delete removes files deleted here; excluded paths (venv, data, .env, git) are never touched on the Pi.
rsync -az --delete \
    --exclude '.git/' --exclude '.venv/' --exclude 'data/' --exclude '.env' \
    --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '.DS_Store' \
    --exclude '.vscode/' --exclude '.micropico' \
    ./ "$USER_AT:$DEST/"

if [[ "$COPY_ENV" == 1 ]]; then
    [[ -f .env ]] || { echo "no .env here to copy" >&2; exit 1; }
    echo "==> copying .env (mode 600; value not shown)"
    scp -q .env "$USER_AT:$DEST/.env"
    ssh "$USER_AT" "chmod 600 $DEST/.env"
fi

if [[ "$SETUP" == 1 ]]; then
    echo "==> running pi_setup.sh on the Pi"
    ssh -t "$USER_AT" "cd $DEST && ./deploy/pi_setup.sh"
elif [[ "$RESTART" == 1 ]]; then
    echo "==> restarting the service"
    ssh -t "$USER_AT" "sudo systemctl restart breathwatch && systemctl is-active breathwatch"
fi
echo "==> done: http://$HOST:8000"
