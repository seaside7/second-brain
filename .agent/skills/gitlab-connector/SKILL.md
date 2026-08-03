# GitLab Connector Skill

Interact with your self-hosted or cloud GitLab instance — list projects, read issues and merge requests, search code, and manage issues.

## Capabilities

1. **List projects** — your projects (or a group's projects)
2. **List issues** — issues in a project, with filters (state, label, assignee)
3. **Get issue** — full details of a single issue
4. **Create issue** — open a new issue (requires `--approved`)
5. **List merge requests** — MRs in a project
6. **Get merge request** — full details + approvals of a single MR
7. **Search** — full-text code or issue search across projects
8. **Pipelines** — list recent CI/CD pipeline runs for a project
9. **List groups** — all groups you have access to

## Setup

### 1. Create a Personal Access Token

1. Go to your GitLab instance → **Preferences → Access Tokens**
   (e.g. `https://gitlab.catalyze.id/-/user_settings/personal_access_tokens`)
2. Create a token named `ai-second-brain`
3. Scopes needed: `read_api` (read-only) or `api` (if you want to create issues)
4. Copy the token

### 2. Save credentials

Create:
```
.agent/skills/gitlab-connector/token.env
```

Contents:
```
GITLAB_URL=https://gitlab.catalyze.id
GITLAB_TOKEN=your-personal-access-token-here
```

If you use gitlab.com instead of a self-hosted instance, use:
```
GITLAB_URL=https://gitlab.com
```

## Usage

```bash
# List groups you belong to
python .agent/skills/gitlab-connector/scripts/gitlab_client.py --action list_groups

# List projects (defaults to your own, or use --group-id)
python .agent/skills/gitlab-connector/scripts/gitlab_client.py --action list_projects
python .agent/skills/gitlab-connector/scripts/gitlab_client.py --action list_projects --group-id 123

# List issues in a project
python .agent/skills/gitlab-connector/scripts/gitlab_client.py --action list_issues --project-id 456
python .agent/skills/gitlab-connector/scripts/gitlab_client.py --action list_issues --project-id 456 --state opened --labels "bug,urgent"

# Get full issue details
python .agent/skills/gitlab-connector/scripts/gitlab_client.py --action get_issue --project-id 456 --iid 12

# Create an issue (requires --approved)
python .agent/skills/gitlab-connector/scripts/gitlab_client.py --action create_issue --project-id 456 --title "Fix login bug" --approved
python .agent/skills/gitlab-connector/scripts/gitlab_client.py --action create_issue --project-id 456 --title "..." --description "..." --labels "bug" --approved

# List merge requests
python .agent/skills/gitlab-connector/scripts/gitlab_client.py --action list_mrs --project-id 456
python .agent/skills/gitlab-connector/scripts/gitlab_client.py --action list_mrs --project-id 456 --state merged

# Get a single MR
python .agent/skills/gitlab-connector/scripts/gitlab_client.py --action get_mr --project-id 456 --iid 78

# Search code across all projects
python .agent/skills/gitlab-connector/scripts/gitlab_client.py --action search --query "def authenticate"

# List pipelines for a project
python .agent/skills/gitlab-connector/scripts/gitlab_client.py --action pipelines --project-id 456
```

## Approval Gate

`create_issue` modifies GitLab state. It requires `--approved`.
Only pass `--approved` after the owner has explicitly signed off.

## Credentials

- `token.env` — stores URL + token (gitignored, stays local)
- Personal Access Token is static, no OAuth flow needed
