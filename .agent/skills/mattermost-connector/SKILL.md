# Mattermost Connector Skill

Interact with your Mattermost workspace at Catalyze — read channels, fetch message history, search messages, and post (with explicit approval gate).

## Capabilities

1. **List channels** — all public channels in a team
2. **Read history** — messages from any channel
3. **Search messages** — full-text search across the workspace
4. **Get user info** — resolve a username or user ID
5. **Post message** — send to a channel or thread (requires `--approved`)
6. **List direct messages** — DM conversations

## Setup

### 1. Get your Personal Access Token

1. Log into your Mattermost instance (e.g. `https://mattermost.catalyze.id`)
2. Go to **Profile → Security → Personal Access Tokens**
3. Click **Create Token** — name it `ai-second-brain`
4. Copy the token (shown only once)

If Personal Access Tokens are disabled by your admin, use a **bot account token** instead (System Console → Integrations → Bot Accounts).

### 2. Find your Team ID

You need the team name or ID. Run:
```bash
python .agent/skills/mattermost-connector/scripts/mattermost_client.py --action list_teams
```
It will print all teams with their IDs.

### 3. Save credentials

Create the token file:
```
.agent/skills/mattermost-connector/token.env
```

Contents:
```
MATTERMOST_URL=https://mattermost.catalyze.id
MATTERMOST_TOKEN=your-personal-access-token-here
MATTERMOST_TEAM_ID=your-team-id-here
```

## Usage

```bash
# List all teams (use this first to get your team ID)
python .agent/skills/mattermost-connector/scripts/mattermost_client.py --action list_teams

# List public channels in your team
python .agent/skills/mattermost-connector/scripts/mattermost_client.py --action list_channels

# Read last 20 messages from a channel
python .agent/skills/mattermost-connector/scripts/mattermost_client.py --action history --channel <channel-id>

# Read history with more messages
python .agent/skills/mattermost-connector/scripts/mattermost_client.py --action history --channel <channel-id> --limit 50

# Search messages
python .agent/skills/mattermost-connector/scripts/mattermost_client.py --action search --query "deployment"

# Get info on a user
python .agent/skills/mattermost-connector/scripts/mattermost_client.py --action user_info --user <user-id-or-username>

# List your direct message channels
python .agent/skills/mattermost-connector/scripts/mattermost_client.py --action list_dms

# Post a message (requires --approved)
python .agent/skills/mattermost-connector/scripts/mattermost_client.py --action post --channel <channel-id> --text "Hello" --approved
```

## Approval Gate

`post` sends to Mattermost. It is blocked by default and requires `--approved`.
Only pass `--approved` after the owner has explicitly signed off on that specific draft.
Never pass it speculatively.

## Credentials

- `token.env` — stores URL, token, team ID (gitignored, stays local)
- No OAuth flow needed — Personal Access Tokens are static
