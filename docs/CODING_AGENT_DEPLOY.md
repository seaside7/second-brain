# Coding Agent - VPS Deployment Notes

How to deploy the Coding Agent (OpenCode-backed coding jobs) to the headless
VPS. Read CLAUDE.md "Remote Deployment & VPS Access" first for the baseline
deploy flow. All VPS paths below are on `ubuntu@43.157.241.209`.

## 1. Ship the code (standard flow)

```bash
git add <intended files>; git commit -m "coding agent: ..."; git push origin main
ssh ubuntu@43.157.241.209 "cd /home/ubuntu/projects/second-brain && git pull"
```

Intended files this feature touches:
- `dashboard/coding_agent.py` (new)
- `dashboard/server.py` (import + routes + background sweep)
- `dashboard/public/tab-coding.js` (new)
- `dashboard/public/index.html`, `dashboard/public/app.js`, `dashboard/public/style.css`
- `.env.example` (documentation only)

No new Python deps - `requests` is already in requirements.txt.

## 2. Copy `.env` from local

The machine-local `.env` (gitignored) carries dashboard settings the VPS needs.
scp it over (key auth, `BatchMode=yes` compatible):

```bash
scp .env ubuntu@43.157.241.209:/home/ubuntu/projects/second-brain/.env
```

> ⚠ Do NOT copy gitignored state (journal/, .agent/) - the VPS keeps its own.
> Do NOT commit `.env` - it stays out of git on both machines.

## 3. Add the Coding Agent env block to systemd

`second-brain-dashboard.service` runs as `User=ubuntu` (HOME=/home/ubuntu), so
spawned opencode servers inherit the VPS's `~/.config/opencode` +
`~/.local/share/opencode/auth.json` (provider credentials reused, none passed
by us). Add an `Environment=` block for every new var:

```ini
[Service]
Environment=CODING_PROJECTS_ROOT=/home/ubuntu/projects
Environment=CODING_WORKTREES_ROOT=/home/ubuntu/projects/.worktrees
Environment=CODING_OPENCODE_BIN=/home/ubuntu/.opencode/bin/opencode
Environment=CODING_OPENCODE_MODE=per-job
Environment=OPENCODE_SERVER_USERNAME=opencode
Environment=OPENCODE_SERVER_PASSWORD=<generate a random password>
Environment=CODING_PORT_RANGE=4100-4199
Environment=CODING_JOB_TTL_HOURS=72
Environment=CODING_PREVIEW_TTL_SECS=1800
Environment=CODING_GIT_NAME=Second Brain Coding Agent
Environment=CODING_GIT_EMAIL=said@catalyze.id
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl restart second-brain-dashboard
```

Notes:
- `CODING_PROJECTS_ROOT` = `/home/ubuntu/projects` (decision made for the VPS;
  repos already cloned there, e.g. `sea-map-cms`, `crsv-kef-api`).
- The opencode binary is `${HOME}/.opencode/bin/opencode` on the VPS and is NOT
  on systemd's PATH - the absolute path above is required.
- Prefer per-job mode (default). `CODING_OPENCODE_MODE=single` only applies
  when the user previously left a dedicated opencode server running.

## 4. Verify

```bash
curl -s http://127.0.0.1:3737/api/coding/repos | jq '{configured, repos: [.repos[].name]}'
```

Expected: `configured: true` and the list of repos you have under
`/home/ubuntu/projects`. The dashboard binds 0.0.0.0:3737 but the port is
iptables/ufw-blocked from the public internet (2026-08 hardening); only
127.0.0.1 requests (or the SSH tunnel) reach it.

Manual smoke job: create a plan job via the UI at
`http://localhost:3737` (over the SSH tunnel), approve build, commit, push.

## Notes / gotchas
- Per-job servers bind 127.0.0.1 on ports from CODING_PORT_RANGE (4100-4199).
- Default branch for coding jobs is `staging` (never main/master/develop). When
  `staging` is already checked out by an active older job, the new job is
  refused with a clear error; terminal/stopped jobs' worktrees are reclaimed
  automatically.
- Commit identity falls back to `Second Brain Coding Agent <said@catalyze.id>`;
  do NOT inherit OWNER_EMAIL from the local `.env` (placeholder value
  `you@example.com` would leak into commits).
- State lives in `journal/state/coding_jobs.json` (gitignored, VPS-local).