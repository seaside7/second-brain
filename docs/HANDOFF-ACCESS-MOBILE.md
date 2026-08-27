# Remote Access Handoff - Second Brain Dashboard (Mobile)

> When you are away from your normal Wi-Fi / on a phone / moving between networks,
> your IP changes constantly, so the dashboard's static IP allowlist
> (`DASHBOARD_ALLOWED_IPS`) will NOT let you through. The stable way in is an
> **SSH local port-forward**, which does not touch the allowlist at all.

## Why SSH tunnel
- The dashboard server already trusts `127.0.0.1` by default (see
  `dashboard/server.py::_build_allowed_ips` - it always adds `127.0.0.1` and `::1`).
- An SSH tunnel forwards a local port to the server, so your browser hits
  `localhost` on the VPS -> the allowlist admits it -> **your changing IP is
  irrelevant**. Only your SSH key matters.
- The whole pipe is encrypted, so nothing sensitive (dashboard content, tokens,
  invoice PDFs) crosses the network in clear text.
- No domain needed, no public port opened, no firewall changes.

## Server details (verify with `ssh ubuntu@43.157.241.209`)
- VPS IP: `43.157.241.209`
- SSH user: `ubuntu` (key-based auth; `BatchMode=yes` works, no password prompt)
- Dashboard service: `second-brain-dashboard.service` (systemd)
- Dashboard binds `0.0.0.0:3737`; default port is `3737`
  (`dashboard/server.py:34`, `DASHBOARD_PORT` env, default `3737`)
- The systemd unit does NOT pass a `--port`, so it runs on `3737`.
- Restart command: `sudo systemctl restart second-brain-dashboard`
  (on VPS: `cd /home/ubuntu/projects/second-brain`)

## Quick start (CLI, one line on any machine)
```bash
ssh -N -L 3737:127.0.0.1:3737 ubuntu@43.157.241.209
```
Leave that running, then open `http://localhost:3737` in your browser.

## Setting up on a Mac (GUI - Termius or similar)
1. Install **Termius** (or any SSH client with port-forwarding).
2. New host:
   - Host: `43.157.241.209`
   - User: `ubuntu`
   - Auth: SSH key (or your password if you set one)
3. Enable **Local port forwarding**:
   - Local port: `3737`
   - Destination host: `127.0.0.1`
   - Destination port: `3737`
4. Connect, then open `http://localhost:3737` in Safari/Chrome.

### macOS native (no extra app) - one-liner in Terminal:
```bash
ssh -N -L 3737:127.0.0.1:3737 ubuntu@43.157.241.209
```

## What the tunnel keeps working while mobile
- The dashboard (all tabs, incl. the **Invoice** tab + PDF download)
- Any action buttons that call local scripts on the VPS
- `/api/invoices`, `/api/invoice/generate`, `/api/invoice/file`

## Notes / gotchas
- The dashboard has NO authentication of its own - the SSH tunnel is what protects
  it. Never open port `3737` to the public internet directly.
- If the tunnel drops (network change), just re-run the SSH command; the VPS
  service stays up independently (systemd `Restart=always`).
- Longer-term alternative: Tailscale / Cloudflare Tunnel for a persistent named URL
  that also survives IP changes (heavier install; only if mobile access becomes a
  regular need).
