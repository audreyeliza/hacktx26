# Respiro public demo — deployment notes (Vultr Ubuntu + Caddy + .TECH domain)

**Deployed** (Sun 2026-10-25, 00:2x UTC) to the team's Vultr server — Ubuntu 24.04.5, Dallas,
1 vCPU / 1 GB — at **https://149-28-251-200.sslip.io** (sslip.io resolves the name to the IP, so
HTTPS works before the .TECH domain exists; switching is one line, see §5). Below are the steps
as actually run.
The public demo runs with `PUBLIC_DEMO=1` (AGENTS.md §20): simulated beds only, no UDP/serial
listener, admin locked, demo sign-in buttons only, Gemini rate-limited, a "Public demo —
simulated data, research prototype" banner on every page, and the app bound to `127.0.0.1`
behind Caddy (HTTPS).

Research prototype — not a medical device. Never put real patient data on the public demo.

## 0. What you need

- A Vultr account and an SSH key on your Mac (`~/.ssh/id_ed25519.pub`).
- A `.TECH` domain (MLH / get.tech) and access to its DNS settings.
- The Gemini API key (it goes into `/opt/breathwatch/.env` on the server; never into git).
- In Google AI Studio, a **budget / quota limit** on the key you use for the public demo (the app
  limits summaries to 6 per session and 60 per hour overall, but a hard cap at Google is the
  real protection). Consider a separate key for the public demo.

## 1. Create the server (Vultr)

- Cloud Compute, Regular Performance, **1 vCPU / 1 GB RAM** is enough (6 simulated beds use well
  under one core; the ward is capped at 12 patients in public mode).
- Image: **Ubuntu 24.04 LTS x64** (Python 3.12; the deployed server has 3.12.3). Ubuntu 26.04 LTS (Python 3.14) also works: the
  pinned requirements resolve for both.
- Add your SSH key, enable IPv6 if you want an AAAA record, note the server's IPv4 address.

## 2. Point the .TECH domain at it (later; the sslip.io name works without DNS)

In the domain's DNS panel (get.tech or wherever the domain's nameservers are):

| Type | Host | Value | TTL |
|---|---|---|---|
| A | `demo` (or `@` for the bare domain) | the server's IPv4 | 300 |
| AAAA | `demo` (optional) | the server's IPv6 | 300 |

Check from the Mac before going further (Caddy needs this to issue the certificate):

```bash
dig +short demo.yourname.tech        # should print the server's IPv4
```

## 3. Base setup (on the server, as root once)

```bash
ssh root@SERVER_IP
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
apt-get update && apt-get -y upgrade && apt-get install -y python3-venv python3-dev rsync
adduser --system --group --home /opt/breathwatch breathwatch          # runs the app, no login
adduser --disabled-password --gecos "" audreybolyard                  # admin SSH login, key only
usermod -aG sudo audreybolyard
install -d -m 700 -o audreybolyard -g audreybolyard /home/audreybolyard/.ssh
install -m 600 -o audreybolyard -g audreybolyard /root/.ssh/authorized_keys /home/audreybolyard/.ssh/authorized_keys
echo 'audreybolyard ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/90-audreybolyard   # no password exists to type
chmod 440 /etc/sudoers.d/90-audreybolyard && visudo -cf /etc/sudoers.d/90-audreybolyard
```

**Check key login works for the new user before the next step** (from the Mac):
`ssh audreybolyard@SERVER_IP 'whoami; sudo -n true && echo sudo ok'`. Then turn off password
login. Vultr's cloud-init file sets `PasswordAuthentication yes`; sshd uses the first value it
reads, so a drop-in that sorts first wins:

```bash
printf 'PasswordAuthentication no\nKbdInteractiveAuthentication no\n' | sudo tee /etc/ssh/sshd_config.d/00-breathwatch-hardening.conf
sudo sshd -t && sudo sshd -T | grep -E '^(passwordauthentication|kbdinteractiveauthentication)'
sudo systemctl reload ssh
# from the Mac: a password attempt must now fail with "Permission denied (publickey)"
# then, after confirming `ssh audreybolyard@SERVER_IP 'sudo -n true'` works, turn off root login:
echo 'PermitRootLogin no' | sudo tee -a /etc/ssh/sshd_config.d/00-breathwatch-hardening.conf
sudo sshd -t && sudo systemctl reload ssh      # root@SERVER_IP now gets "Permission denied"
```

Firewall (Vultr images already have ufw active with 22/tcp allowed):

```bash
sudo ufw allow 80/tcp && sudo ufw allow 443/tcp && sudo ufw status
```

Only SSH, 80 and 443 are open. Port 8000 is never exposed (the app listens on 127.0.0.1), and
UDP 5005 stays closed (public mode does not listen for a wearable at all).

## 4. Install Respiro

Copy the code from the Mac (never the Mac's `.env`; the public demo gets its own key):

```bash
# on the Mac, from the repo root
rsync -az --delete --rsync-path="sudo rsync" --exclude .git/ --exclude .venv/ --exclude data/ --exclude .env \
      --exclude __pycache__/ --exclude .pytest_cache/ --exclude .DS_Store --exclude .vscode/ --exclude .micropico \
      ./ audreybolyard@SERVER_IP:/opt/breathwatch/
```

Then on the server:

```bash
sudo install -d -o breathwatch -g breathwatch /opt/breathwatch/data/.matplotlib
[ -f /opt/breathwatch/.env ] || printf 'GEMINI_API_KEY=\n' | sudo tee /opt/breathwatch/.env >/dev/null   # empty placeholder
sudo chown -R breathwatch:breathwatch /opt/breathwatch && sudo chmod 600 /opt/breathwatch/.env
sudo -u breathwatch python3 -m venv /opt/breathwatch/.venv
sudo -u breathwatch /opt/breathwatch/.venv/bin/pip install -q --no-cache-dir --upgrade pip
sudo -u breathwatch /opt/breathwatch/.venv/bin/pip install -q --no-cache-dir -r /opt/breathwatch/requirements.txt
sudo install -m 644 /opt/breathwatch/deploy/breathwatch-public.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now breathwatch-public
curl -s http://127.0.0.1:8000/api/public-config   # {"public_demo": true, ...}
```

Startup takes **~16 s** on 1 vCPU (each simulated bed is backfilled with 150 s of history);
`curl` is refused until `Application startup complete` appears in `journalctl -u breathwatch-public`.
The app uses ~115 MB of RAM.

## 5. Caddy (HTTPS)

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
grep -v '^\s*#' /opt/breathwatch/deploy/Caddyfile.example | sudo tee /etc/caddy/Caddyfile >/dev/null   # line 1 = site address
sudo install -d -o caddy -g caddy /var/log/caddy
sudo -u caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile   # as caddy, see below
sudo systemctl reload caddy
```

Gotcha (hit on the real deploy): `sudo caddy validate` **as root** creates
`/var/log/caddy/breathwatch.log` owned by root, and the reload then fails ("opening log writer").
Validate as the `caddy` user as above, or `sudo chown caddy:caddy /var/log/caddy/breathwatch.log`.

Caddy obtains a Let's Encrypt certificate right after the reload; open
`https://tryrespiro.tech` (also `https://149-28-251-200.sslip.io/login`) and use a **Demo sign-in** button.

**Wearable on the public site** (`PUBLIC_DEMO_WEARABLE=1` in `deploy/breathwatch-public.service`): the live bed stays `patch-01` and the app listens for UDP on port 5005. Open it once: `sudo ufw allow 5005/udp`. On the Pico, `secrets.py` `SERVER_IP` is `149.28.251.200`, and the Pico's Wi-Fi must reach the public internet. USB serial still only reaches the computer the Pico is plugged into. Source switching, serial listing, and recording stay refused.

**Switching to the .TECH domain (one line):** add the A record (§2), check `dig +short`, then

```bash
sudo sed -i '1s/.*/demo.yourname.tech {/' /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

(or `149-28-251-200.sslip.io, demo.yourname.tech {` on line 1 to serve both while switching). The browser must show a
valid certificate (the session cookie is `Secure` in public mode, so plain HTTP cannot sign in).

## 6. Checks after deploying

- `https://…/login` shows the yellow banner and only the demo sign-in buttons work.
- The ward shows six simulated beds with rates (backfilled history); bed 4 runs the `demo` scenario.
- Admin → Device & data: source switching and recording are refused ("disabled in the public demo").
- A second demo reset within 2 minutes is refused (429).
- AI panel: a summary appears ("Powered by Gemini"); after 6 in an hour it serves the cached copy.
- `ss -lntup` on the server: only Caddy on :80/:443 and Python on 127.0.0.1:8000.

## 7. Adding the public-demo Gemini key

Until a key is set, every summary answers `503 "Gemini unavailable (GEMINI_API_KEY is not set …)
and no cached … summary yet"` and the frontend shows that the language model is unreachable.

```bash
ssh audreybolyard@SERVER_IP
sudoedit /opt/breathwatch/.env                # set GEMINI_API_KEY=… (keeps owner breathwatch, mode 600)
sudo systemctl restart breathwatch-public     # optional: the key is read again on the next summary anyway
```

Use a separate key with a budget cap in Google AI Studio.

## 8. Day-to-day

```bash
journalctl -u breathwatch-public -f           # app log (the key is never logged)
journalctl -u caddy -f
systemctl restart breathwatch-public          # e.g. after copying new code
```

Update code: rsync again (step 4, without `.env`), `chown -R breathwatch:breathwatch /opt/breathwatch`,
`.venv/bin/pip install -r requirements.txt` if requirements changed, then restart.

To take the demo down: `systemctl disable --now breathwatch-public caddy`, and delete the DNS
records. Revoke the public-demo Gemini key if it is no longer needed.
