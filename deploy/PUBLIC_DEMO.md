# BreathWatch public demo — deployment notes (Vultr Ubuntu + Caddy + .TECH domain)

**Not deployed yet.** These are the steps for when the team decides to put the demo online.
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
- Image: **Ubuntu 24.04 LTS x64** (Python 3.12). Ubuntu 26.04 LTS (Python 3.14) also works: the
  pinned requirements resolve for both.
- Add your SSH key, enable IPv6 if you want an AAAA record, note the server's IPv4 address.

## 2. Point the .TECH domain at it

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
apt update && apt upgrade -y
apt install -y python3-venv python3-dev git ufw
adduser --system --group --home /opt/breathwatch breathwatch
ufw allow OpenSSH && ufw allow 80/tcp && ufw allow 443/tcp && ufw --force enable
```

Only SSH, 80 and 443 are open. Port 8000 is never exposed (the app listens on 127.0.0.1), and
UDP 5005 stays closed (public mode does not listen for a wearable at all).

## 4. Install BreathWatch

Copy the code (from git, or rsync from the Mac):

```bash
# on the Mac, from the repo root
rsync -az --exclude .git/ --exclude .venv/ --exclude data/ --exclude .env \
      ./ root@SERVER_IP:/opt/breathwatch/
scp .env root@SERVER_IP:/opt/breathwatch/.env          # GEMINI_API_KEY=...
```

Then on the server:

```bash
cd /opt/breathwatch
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
mkdir -p data/.matplotlib
chown -R breathwatch:breathwatch /opt/breathwatch
chmod 600 /opt/breathwatch/.env
install -m 644 deploy/breathwatch-public.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now breathwatch-public
systemctl status breathwatch-public           # "PUBLIC DEMO mode: ..." in the log
curl -s http://127.0.0.1:8000/api/public-config   # {"public_demo": true, ...}
```

## 5. Caddy (HTTPS)

```bash
apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
apt update && apt install -y caddy
cp /opt/breathwatch/deploy/Caddyfile.example /etc/caddy/Caddyfile
nano /etc/caddy/Caddyfile                     # replace demo.example.tech with your domain
mkdir -p /var/log/caddy && chown caddy:caddy /var/log/caddy
caddy validate --config /etc/caddy/Caddyfile
systemctl reload caddy
```

Caddy requests a Let's Encrypt certificate on the first request; open
`https://demo.yourname.tech/login` and use a **Demo sign-in** button. The browser must show a
valid certificate (the session cookie is `Secure` in public mode, so plain HTTP cannot sign in).

## 6. Checks after deploying

- `https://…/login` shows the yellow banner and only the demo sign-in buttons work.
- The ward shows six simulated beds with rates (backfilled history); bed 4 runs the `demo` scenario.
- Admin → Device & data: source switching and recording are refused ("disabled in the public demo").
- A second demo reset within 2 minutes is refused (429).
- AI panel: a summary appears ("Powered by Gemini"); after 6 in an hour it serves the cached copy.
- `ss -lntup` on the server: only Caddy on :80/:443 and Python on 127.0.0.1:8000.

## 7. Day-to-day

```bash
journalctl -u breathwatch-public -f           # app log (the key is never logged)
journalctl -u caddy -f
systemctl restart breathwatch-public          # e.g. after copying new code
```

Update code: rsync again (step 4, without `.env`), `chown -R breathwatch:breathwatch /opt/breathwatch`,
`.venv/bin/pip install -r requirements.txt` if requirements changed, then restart.

To take the demo down: `systemctl disable --now breathwatch-public caddy`, and delete the DNS
records. Revoke the public-demo Gemini key if it is no longer needed.
