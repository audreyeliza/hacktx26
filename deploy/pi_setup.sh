#!/usr/bin/env bash
# One-time (and repeatable) setup of BreathWatch on the Raspberry Pi 4.
# Run ON THE PI, from the project folder:
#     cd ~/hacktx && ./deploy/pi_setup.sh            # install deps, venv, systemd service, start it
#     ./deploy/pi_setup.sh --test                    # also run the test suite first (slow on a Pi 4)
# Expects: Raspberry Pi OS Lite 64-bit (Trixie), Python 3.13.x, user audreybolyard, ~/hacktx.
# Safe to re-run: it updates the venv and restarts the service.
set -euo pipefail

APP_DIR="/home/audreybolyard/hacktx"
APP_USER="audreybolyard"
SERVICE="breathwatch"
RUN_TESTS=0
[[ "${1:-}" == "--test" ]] && RUN_TESTS=1

say() { printf '\n==> %s\n' "$*"; }

if [[ "$(id -un)" != "$APP_USER" ]]; then
    echo "Run this as $APP_USER (not root); it uses sudo where needed." >&2
    exit 1
fi
if [[ "$(pwd)" != "$APP_DIR" ]]; then
    echo "Run this from $APP_DIR (currently in $(pwd))." >&2
    exit 1
fi

say "Checking Python"
PY=/usr/bin/python3
"$PY" - <<'EOF'
import sys
v = sys.version_info
print(f"python {v.major}.{v.minor}.{v.micro}")
if v < (3, 11):
    sys.exit("BreathWatch needs Python 3.11 or newer")
EOF

say "System packages (python3-venv)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-dev

say "Serial access: adding $APP_USER to the dialout group (for the Pico on USB)"
sudo usermod -aG dialout "$APP_USER"

say "Virtual environment and Python packages"
[[ -d .venv ]] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -c "import numpy, scipy, sklearn, fastapi, google.genai; print('imports ok')"

say "Data folder"
mkdir -p data/.matplotlib

say "Gemini key"
if [[ -f .env ]]; then
    chmod 600 .env
    if grep -q '^GEMINI_API_KEY=.\+' .env; then echo ".env has GEMINI_API_KEY (value not shown)"; else echo ".env has no GEMINI_API_KEY value: summaries will use the cache / return 503"; fi
else
    echo "No .env: summaries will use the cache / return 503 (copy it with deploy/push_to_pi.sh --env)"
fi

if [[ "$RUN_TESTS" == 1 ]]; then
    say "Test suite (this takes several minutes on a Pi 4)"
    .venv/bin/pytest -q tests
fi

say "systemd service"
sudo install -m 644 deploy/breathwatch.service /etc/systemd/system/$SERVICE.service
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null
sudo systemctl restart "$SERVICE"
sleep 4
if systemctl is-active --quiet "$SERVICE"; then
    echo "$SERVICE is running."
else
    echo "$SERVICE failed to start; last log lines:" >&2
    sudo journalctl -u "$SERVICE" -n 30 --no-pager >&2
    exit 1
fi

say "Done"
echo "Dashboard:   http://$(hostname).local:8000   (or http://<pi-ip>:8000)"
echo "Pico UDP:    set SERVER_IP in the firmware's secrets.py to this Pi's IP; port 5005"
echo "Logs:        journalctl -u $SERVICE -f"
echo "Note: the dialout group applies to new logins; the service already has it (SupplementaryGroups)."
