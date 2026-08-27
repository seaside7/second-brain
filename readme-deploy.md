# Deploy AI Second Brain on an Ubuntu VPS

This guide deploys the AI Second Brain harness to a headless Ubuntu VPS: the
**dashboard** (`http://localhost:3737`), the **cron layer** that feeds it, and the
**connectors** that talk to Google, Slack, and the rest.

It assumes a fresh Ubuntu 20.04 / 22.04 / 24.04 server with root access over SSH.
Deploying to a VPS is different from a laptop in three ways that this doc exists to
solve:

1. **It is headless** - no browser, so the Google OAuth flow needs the console
   (copy/paste) method, not the localhost popup method.
2. **It is a server** - the dashboard must run as a service, behind a reverse proxy,
   not as a `python3 ...` you keep in a terminal.
3. **Credentials do not travel with `git`** - `.env`, `token.env`, `credentials.json`,
   and `token.json` are gitignored on purpose. You must copy or re-create them on the
   VPS or the harness runs blind and the dashboard shows empty panels.

---

## Table of Contents

1. [What you are deploying](#1-what-you-are-deploying)
2. [Server prerequisites](#2-server-prerequisites)
3. [First-time server prep](#3-first-time-server-prep)
4. [Clone and install](#4-clone-and-install)
5. [Environment and credentials - READ THIS](#5-environment-and-credentials---read-this)
6. [Google OAuth on a headless server](#6-google-oauth-on-a-headless-server)
7. [Run the dashboard as a service](#7-run-the-dashboard-as-a-service)
8. [Reverse proxy with TLS (optional but recommended)](#8-reverse-proxy-with-tls-optional-but-recommended)
9. [Cron jobs](#9-cron-jobs)
10. [Optional: meetbot and the meeting recorder](#10-optional-meetbot-and-the-meeting-recorder)
11. [Updating the deployment](#11-updating-the-deployment)
12. [Verify everything](#12-verify-everything)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. What you are deploying

| Component | What it is | How it runs on the VPS |
| :--- | :--- | :--- |
| **Dashboard** | Python stdlib web server on port `3737` (`dashboard/server.py`) | `systemd` service |
| **Cron layer** | ~dozens of `crontab` entries that refresh inbox, tracker, health, news | `crontab -e` |
| **Connectors** | Skill scripts that call Google/Slack/Fathom/etc. | Invoked by cron + dashboard |
| **meetbot** (optional) | Rust bot, port `8060`, auto-joins meetings | `systemd` user unit |
| **meeting-recorder** (optional) | Local recorder + transcription | Only useful if the VPS is in your meetings |

The dashboard reads your repo files live on every request, so nothing else needs to
"run" - just the server and the crons.

---

## 2. Server prerequisites

| Requirement | Minimum | Notes |
| :--- | :--- | :--- |
| OS | Ubuntu 20.04+ (22.04/24.04 recommended) | |
| RAM | 2 GB (4 GB if you run meetbot/Playwright) | Dashboard itself is tiny |
| Python | 3.8+ | `apt install python3 python3-pip python3-venv` |
| Node.js | 18+ | Only needed for the Claude Code CLI, see below |
| Storage | 2 GB free | Playwright Chromium is ~150 MB on top |

**Do you need Claude Code on the server?** Only if the cron jobs or the dashboard's
AI-task buttons should run AI. The dashboard degrades gracefully without the `claude`
CLI (`dashboard/server.py:27` resolves a backend-agnostic runner). If you do want AI
calls on the server, install it (`npm install -g @anthropic-ai/claude-code`) and
authenticate once with `claude`, because crons and the service run as a service user,
not as you.

---

## 3. First-time server prep

```bash
# 1. Update the system
sudo apt update && sudo apt upgrade -y

# 2. Base packages
sudo apt install -y git curl python3 python3-pip python3-venv build-essential \
    ca-certificates gnupg

# 3. Optional but recommended for connectors:
#    ffmpeg - meeting recorder / transcription
#    chromium or `playwright install chromium` - browser-service (WhatsApp, SEO)
sudo apt install -y ffmpeg

# 4. Firewall: only open what you need. Dashboard is NOT meant to be public.
sudo ufw allow OpenSSH
sudo ufw enable
sudo ufw status
```

Create a **non-root user** to run the harness. Running services as root is a bad idea
and the Google OAuth tokens are stored in plain files, so the user should own them:

```bash
sudo adduser brain              # any username is fine
sudo usermod -aG sudo brain
sudo -i -u brain bash           # do everything below as this user
```

---

## 4. Clone and install

```bash
cd ~
git clone https://github.com/BrianArfi/ai-second-brain.git
cd ai-second-brain

# Python deps
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
# Optional: browser automation for WhatsApp/SEO connectors
# playwright install chromium

# One-command bootstrap (CLAUDE.md + .env from template)
bash install.sh
```

`install.sh` creates `CLAUDE.md` (from `CLAUDE.md.template`) and `.env` (from
`.env.example`) if they do not exist, and never overwrites files you already made.
The harness detects the OS at session start, so the same repo that runs on your laptop
runs here unchanged.

> **Before you go further: read section 5. The most common deployment mistake is
> skipping the credential copy step and ending up with a dashboard that shows empty
> panels and cron jobs that fail with `401` / `not found`.**

---

## 5. Environment and credentials - READ THIS

Credentials never leave your machine via git. `.gitignore` blocks `.env`, `*.env`,
`token.json`, `credentials.json`, `cred.txt`, and the workspace credential files. On
the VPS you must **copy or re-create** the files below. Missing files are the number
one cause of "it worked on my laptop" failures.

### 5.1 The credential inventory

| File | What is in it | Needed for | Created how |
| :--- | :--- | :--- | :--- |
| `.env` (repo root) | Identity + API keys (Slack, Fathom, Mixpanel, Figma, ClickUp, Telegram, DeepSeek, OpenAI) | Everything | `cp .env.example .env` (already done by `install.sh`) |
| `.agent/skills/<connector>/token.env` | One `KEY=value` per connector (Slack, Fathom, Figma, Mixpanel, ClickUp, GitLab, Mattermost, Trello, agy-bridge) | Each connector individually | `echo "KEY=value" > .agent/skills/<connector>/token.env` |
| `.agent/skills/<connector>/credentials.json` | Google OAuth **client** ID + secret (not secret yet, but keep private) | Every Google skill | Download from Google Cloud Console (see section 6) |
| `.agent/skills/<connector>/token.json` | Google OAuth **access/refresh** token, auto-generated | Every Google skill | Auto-created on first auth |
| `credentials.json` (repo root) | Google OAuth client, used by the **dashboard** calendar panel | Dashboard calendar | Same client JSON, copied to root |
| `token_calendar.json` (repo root) | Dashboard calendar token | Dashboard calendar | Auto-created on first auth |
| `.agent/workspaces/samudera/token_calendar.json` | Samudera-only calendar token | `/samudera` dashboard view | Auto-created on first auth |
| `meeting-recorder/config.json` | Recorder paths + engine settings | Meeting recorder | `cp meeting-recorder/config.example.json meeting-recorder/config.json` |
| `meeting-recorder/vexa_token.env` | API key shared with meetbot | meetbot auth | You create it |
| `meetbot/config.toml` | meetbot port, API key, browser profile paths | meetbot | `cp meetbot/config.example.toml meetbot/config.toml` |

### 5.2 `.env` - the identity block is not optional

Open `.env` and fix the identity variables first. Several skills use them to answer
"who does this harness work for", and the defaults point at a placeholder. `docs/SETUP.md`
section 6 explains each one; the critical three:

```bash
WORK_DOMAIN=yourcompany.com          # docs get domain-restricted to this
OWNER_EMAIL=you@example.com
OWNER_NAME_TOKENS=jane doe,jane m doe
OWNER_SLACK_ID=<SLACK_ID>            # Slack → Profile → ⋮ → Copy member ID
```

Then add the API keys **only for the connectors you actually use** (section 10 of
`docs/SETUP.md` has a decision tree). You do not need them all.

### 5.3 Per-connector `token.env` files

Each connector reads its own `token.env` from inside its skill folder. The two most
common:

```bash
# Slack
echo "SLACK_BOT_TOKEN=xoxb-..." > .agent/skills/slack-connector/token.env
# or a user token:
echo "SLACK_USER_TOKEN=xoxp-..." > .agent/skills/slack-connector/token.env

# Fathom (cloud meeting transcripts; skip if you only use the local recorder)
echo "FATHOM_API_KEY=your-key" > .agent/skills/fathom-connector/token.env
```

The pattern is identical for `figma-connector`, `mixpanel-connector`,
`clickup-connector`, `gitlab-connector`, `mattermost-connector`, `trello-connector`,
and `agy-bridge`. Keys are documented in `.env.example` and `docs/SETUP.md` section 9.

### 5.4 Checklist - run this after you have copied everything

```bash
cd ~/ai-second-brain

# .env exists and has your identity
test -f .env && echo ".env OK"

# Google OAuth client present for every Google skill you use
ls .agent/skills/work-drive-connector/credentials.json 2>/dev/null || echo "missing work-drive credentials.json"
ls .agent/skills/google-calendar-connector/credentials.json 2>/dev/null || echo "missing calendar credentials.json"
ls credentials.json 2>/dev/null || echo "missing root credentials.json (dashboard calendar)"

# Slack token present (if you use Slack)
test -f .agent/skills/slack-connector/token.env && echo "slack token.env OK"
```

If a line prints `missing ...`, that connector will silently show nothing or throw
`401`/`403`/`Authentication required`. Fix it before moving on.

---

## 6. Google OAuth on a headless server

The Google connector scripts already support a **console flow** for headless machines
(`gdrive_manager.py:68` uses `flow.run_console()` instead of the localhost popup when
there is no browser). You do not need SSH tunnels for this.

### 6.1 Get a `credentials.json` (do this once, on any machine)

Follow `docs/SETUP.md` section 7: create a Google Cloud project, enable Drive / Docs /
Sheets / Calendar / Gmail APIs, configure the consent screen, and create a **Desktop
app** OAuth client. Download the JSON.

> You need a *separate* client per account you connect (work, personal). Name them so
> you can tell them apart, e.g. `credentials-work.json` and `credentials-personal.json`.

### 6.2 Upload and place it on the VPS

```bash
# From your local machine:
scp credentials.json brain@<vps-ip>:~/

# On the VPS, put a copy where each connector expects it.
# The dashboard's calendar panel reads the root copy:
cp ~/credentials.json ~/ai-second-brain/credentials.json

# Google skills read their own copy:
cp ~/credentials.json ~/ai-second-brain/.agent/skills/work-drive-connector/credentials.json
cp ~/credentials.json ~/ai-second-brain/.agent/skills/google-calendar-connector/credentials.json
cp ~/credentials.json ~/ai-second-brain/.agent/skills/gmail-connector/credentials.json   # if used
cp ~/credentials.json ~/ai-second-brain/.agent/skills/personal-drive-connector/credentials.json  # personal account

# Each account you connect needs its own client JSON in the right folder.
```

### 6.3 Run the console auth once

```bash
cd ~/ai-second-brain
source venv/bin/activate

python3 .agent/skills/work-drive-connector/gdrive_manager.py search --query "test"
```

It prints an authorization URL. Open that URL in a browser **on your local machine**
(or on the VPS with `ssh -L`), sign in, and it shows a code. Paste the code back into
the VPS terminal. A `token.json` is written next to the `credentials.json`, and the
connector auto-refreshes it from then on.

Repeat once per connector/account you use. The calendar connector
(`google-calendar-connector`) and the dashboard itself have the same flow - the
dashboard writes `token_calendar.json` at the repo root the first time its calendar
API is hit.

> **Why it matters if you skip this:** without a valid token, cron jobs fail with
> `Authentication required`, and the dashboard's Today / Meetings / calendar panels stay
> empty. This is the single most-missed setup step on a fresh VPS.

---

## 7. Run the dashboard as a service

The dashboard must survive reboots and not depend on your SSH session. Create a
systemd service:

```bash
sudo tee /etc/systemd/system/second-brain-dashboard.service > /dev/null <<'EOF'
[Unit]
Description=AI Second Brain dashboard
After=network.target

[Service]
Type=simple
User=brain
WorkingDirectory=/home/brain/ai-second-brain
ExecStart=/home/brain/ai-second-brain/venv/bin/python3 dashboard/server.py
Restart=on-failure
RestartSec=5

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=/home/brain/ai-second-brain

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now second-brain-dashboard
sudo systemctl status second-brain-dashboard
```

Check it is up:

```bash
curl -s http://127.0.0.1:3737/ | head -c 200
sudo journalctl -u second-brain-dashboard -n 50   # logs if something is wrong
```

**About exposing it.** `dashboard/server.py:5187` binds `0.0.0.0`, but every request
is filtered by an IP allowlist (`127.0.0.1`, `::1`, the WSL gateway, plus anything in
`DASHBOARD_ALLOWED_IPS`). There is **no authentication** on the dashboard, and its
action buttons run local commands. Options, in order of preference:

1. **SSH tunnel (recommended, zero config - and the only option that works when
   your client IP changes, e.g. on mobile / multiple Wi-Fi networks):**
   ```bash
   # from your laptop/Mac/phone, every time you want the dashboard:
   ssh -N -L 3737:127.0.0.1:3737 ubuntu@43.157.241.209
   # then open http://localhost:3737 locally
   ```
   Your laptop's requests arrive as `127.0.0.1` on the server, so the default
   allowlist already admits them. Because this trusts only your SSH key (not your
   IP), you can use it from any network - your changing Wi-Fi IP does not matter.
   See `docs/HANDOFF-ACCESS-MOBILE.md` for GUI (Termius) and macOS-native steps.

2. **Reverse proxy + TLS + your home IP (section 8):** add your home IP to
   `DASHBOARD_ALLOWED_IPS` in the service file:
   ```bash
   Environment=DASHBOARD_ALLOWED_IPS=127.0.0.1,::1,<YOUR_HOME_IP>
   ```

3. **Never** open port 3737 to the public internet directly.

---

## 8. Reverse proxy with TLS (optional but recommended)

If you want the dashboard at `https://brain.example.com` instead of over an SSH tunnel:

```bash
sudo apt install -y nginx certbot python3-certbot-nginx
sudo ufw allow 'Nginx Full'
```

Point your DNS A record at the VPS IP, then:

```bash
sudo tee /etc/nginx/sites-available/brain > /dev/null <<'EOF'
server {
    server_name brain.example.com;

    location / {
        proxy_pass http://127.0.0.1:3737;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
EOF
sudo ln -s /etc/nginx/sites-available/brain /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d brain.example.com
```

Then set `DASHBOARD_ALLOWED_IPS` in the service file to your home IP (the request now
comes from nginx, i.e. `127.0.0.1`, so it is already allowed - but anyone who can
reach the host can proxy through nginx too, so still add Basic Auth at the nginx layer
with `htpasswd` if the dashboard holds anything sensitive). See `docs/DASHBOARD.md` →
Security for the full reasoning.

---

## 9. Cron jobs

The dashboard's tabs are fed by the repo's own crontab entries. On your laptop those
were probably added by the harness itself; on a fresh VPS you need to install them.

```bash
# Back up whatever is there first (never skip this)
crontab -l > ~/crontab.bak.$(date +%F-%H%M)

# Edit
crontab -e
```

Each entry should run from the repo root with the venv python. The minimal set that
keeps the dashboard alive and fed:

```cron
# Dashboard keepalive (from .agent/scripts/dashboard_keepalive.sh)
0 * * * * cd /home/brain/ai-second-brain && bash .agent/scripts/ensure_dashboard.sh

# Daily update runner (tests + runs multiple connectors, 2-3 min)
0 7 * * * cd /home/brain/ai-second-brain && ./venv/bin/python3 .agent/scripts/daily_update_runner.py

# Harness health
*/15 * * * * cd /home/brain/ai-second-brain && ./venv/bin/python3 .agent/scripts/heartbeat.py --job harness-health
```

Cron has a minimal `PATH` and a home of `/root` by default, so always use absolute
paths (`/home/brain/ai-second-brain`) and the venv's python, and add at the top:

```cron
PATH=/home/brain/ai-second-brain/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
HOME=/home/brain
```

For the full list of jobs this repo installs (ledgers, inbox sweep, premeeting cards,
token tracking, portfolio sync, recorder bot watcher), see the "It runs on rails"
section of `README.md`, and check the individual `SKILL.md` files under
`.agent/skills/<name>/` - each documents its own cron cadence. Every registered job
reports a heartbeat; a silent failure shows up on the dashboard's System tab, so use
that to confirm your crontab actually ran.

---

## 10. Optional: meetbot and the meeting recorder

These two are the only components with real per-VPS setup beyond credentials. Only do
this if the VPS is meant to sit in your meetings (meetbot) or record them
(meeting-recorder). A pure dashboard + cron box skips this section.

**meetbot** (`meetbot/`): a Rust service on port `8060` that joins calls headless.

```bash
cd ~/ai-second-brain/meetbot
cp config.example.toml config.toml
# edit: api_key_file, admin_token, data_dir, db_path, profile_template, chromium_path
cargo build --release
./target/release/meetbot doctor <a-meet-code>
```

The browser profile must be **signed in to a real Google account** (seed once with a
visible browser, then point `profile_template` at it). Install `meetbot.service` as a
**systemd user unit** and point the recorder's cron line at it with
`MEETBOT=1 VEXA_API_BASE=http://localhost:8060`. Full detail:
`docs/MEETING_RECORDER.md` section 10.2 and `.agent/skills/meetbot/SKILL.md`.

**meeting-recorder** (`meeting-recorder/`): a headless VPS usually is not where you
sit in meetings, so this is typically only deployed when the VPS hosts meetbot:

```bash
cd ~/ai-second-brain/meeting-recorder
cp config.example.json config.json
# set the right machine section (wsl/macos/windows keyed per detect_platform.sh)
# if you keep whispercpp_bin empty it falls back to the Gemini API for transcription
python3 recorder.py --list-devices
```

---

## 11. Updating the deployment

```bash
cd ~/ai-second-brain
git pull
source venv/bin/activate
pip install -r requirements.txt
# then, if services need a restart:
sudo systemctl restart second-brain-dashboard
```

The harness auto-detects new skills/workflows, but cron entries that the update adds
still need a manual `crontab -e`. Compare against `.agent/scripts/dashboard_keepalive.sh`
and the `SKILL.md` files for anything new.

---

## 12. Verify everything

```bash
# 1. Dashboard
curl -s http://127.0.0.1:3737/api/overview | head -c 300

# 2. Google Drive connector (real auth check)
cd ~/ai-second-brain && source venv/bin/activate
python3 .agent/skills/work-drive-connector/gdrive_manager.py search --query "test"

# 3. Calendar
python3 .agent/skills/google-calendar-connector/gcal_manager.py sweep --profile work --output markdown

# 4. Slack (if configured)
python3 .agent/skills/slack-connector/scripts/slack_client.py --action list_channels

# 5. Daily runner (exercises several connectors at once, 2-3 min)
python3 .agent/scripts/daily_update_runner.py

# 6. Cron health - open the dashboard's System tab; failing/silent jobs show as rows
```

---

## 13. Troubleshooting

| Symptom | Likely cause | Fix |
| :--- | :--- | :--- |
| Dashboard panel empty / "no data" | Connector token missing or expired | Re-run the auth in section 6; check the checklist in 5.4 |
| `Authentication required` in connector output | No `token.json` yet, or it expired | Re-run the console flow; tokens auto-refresh but a revoked token needs a re-auth |
| Cron jobs fail, dashboard System tab shows silent jobs | Absolute paths / PATH not set in crontab | Use `/home/brain/ai-second-brain` paths and the `PATH=` line from section 9 |
| `Connection refused` on `curl 127.0.0.1:3737` | Service not running | `sudo systemctl status second-brain-dashboard`, `sudo journalctl -u second-brain-dashboard -n 50` |
| Dashboard reachable but refuses your browser | IP allowlist | Use the SSH tunnel (section 7.1) or add your IP to `DASHBOARD_ALLOWED_IPS` |
| Google OAuth URL opens but page errors | Client lacks the redirect URI | Desktop clients use console flow; make sure you downloaded a **Desktop app** client JSON |
| Python `No module named X` | venv not used / deps stale | `source venv/bin/activate && pip install -r requirements.txt` |
| meetbot 401s on `vexa_bots.py setup` | `admin_token` in `config.toml` differs from the `.env` | Set both to the same random string |
| Jobs ran before `git pull`, now fail | New job needs new cron line | Re-run `crontab -e`, add the new entries from the update's `SKILL.md` |
