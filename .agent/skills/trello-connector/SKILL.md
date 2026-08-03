# Trello Connector Skill

Interact with Trello boards — list boards, lists, cards, and manage cards (move, comment). Used as a task source for the dev-tracker.

## Capabilities

1. **List boards** — all boards you have access to
2. **List lists** — columns in a board
3. **List cards** — cards in a list or board, with filters
4. **Get card** — full details of a single card
5. **Move card** — move a card to a different list (requires `--approved`)
6. **Add comment** — comment on a card (requires `--approved`)
7. **My cards** — cards assigned to you across all boards

## Setup

### 1. Get your API Key

1. Go to [https://trello.com/power-ups/admin](https://trello.com/power-ups/admin)
2. Create a new Power-Up (or use an existing one)
3. Go to the **API Key** tab → Generate a new API Key
4. Copy the API Key

### 2. Get your Token

1. With your API key, visit:
   ```
   https://trello.com/1/authorize?expiration=never&scope=read,write&response_type=token&key=YOUR_API_KEY
   ```
2. Click **Allow**
3. Copy the token shown on the page

### 3. Save credentials

Create:
```
.agent/skills/trello-connector/token.env
```

Contents:
```
TRELLO_API_KEY=your-api-key-here
TRELLO_TOKEN=your-token-here
```

## Usage

```bash
# List all your boards
python .agent/skills/trello-connector/scripts/trello_client.py --action list_boards

# List columns (lists) in a board
python .agent/skills/trello-connector/scripts/trello_client.py --action list_lists --board-id <id>

# List cards in a specific list
python .agent/skills/trello-connector/scripts/trello_client.py --action list_cards --list-id <id>

# List all cards in a board
python .agent/skills/trello-connector/scripts/trello_client.py --action list_cards --board-id <id>

# Get full card details
python .agent/skills/trello-connector/scripts/trello_client.py --action get_card --card-id <id>

# Cards assigned to me across all boards
python .agent/skills/trello-connector/scripts/trello_client.py --action my_cards

# Move a card to a different list (requires --approved)
python .agent/skills/trello-connector/scripts/trello_client.py --action move_card --card-id <id> --list-id <target-list-id> --approved

# Add a comment to a card (requires --approved)
python .agent/skills/trello-connector/scripts/trello_client.py --action comment --card-id <id> --text "Done, merged to main" --approved
```

## Approval Gate

`move_card` and `comment` modify Trello state. They require `--approved`.

## Credentials

- `token.env` — stores API key + token (gitignored, stays local)
- Token is static (set to never expire), no OAuth refresh needed
