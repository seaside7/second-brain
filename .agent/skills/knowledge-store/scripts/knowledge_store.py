#!/usr/bin/env python3
"""
knowledge_store.py — Long-term organizational memory.

Stores durable knowledge per workspace, auto-classified into category files.
Each entry is a markdown block with metadata. Deduplicated by content similarity.

Storage: .agent/workspaces/<workspace>/knowledge/<category>.md
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, '..', '..', '..', '..'))

sys.path.insert(0, os.path.join(REPO_ROOT, '.agent', 'workspaces'))
import workspace_resolver as ws

WIB = timezone(timedelta(hours=7))

VALID_CATEGORIES = (
    'people', 'projects', 'architecture', 'business', 'decisions',
    'lessons', 'standards', 'troubleshooting', 'glossary', 'processes', 'misc'
)

# UTF-8 output on Windows
try:
    if sys.stdout.encoding != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
    if sys.stderr.encoding != 'utf-8':
        sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# ---------- helpers ----------

def _knowledge_dir(workspace_name):
    """Return the knowledge folder path for a workspace."""
    ctx = ws.get(workspace_name)
    return os.path.join(ctx.dir, 'knowledge')


def _category_file(workspace_name, category):
    """Return the path to a category markdown file."""
    return os.path.join(_knowledge_dir(workspace_name), f'{category}.md')


def _ensure_dir(workspace_name):
    """Create knowledge directory if needed."""
    d = _knowledge_dir(workspace_name)
    os.makedirs(d, exist_ok=True)
    return d


def _content_hash(content):
    """Short hash of content for dedupe."""
    normalized = re.sub(r'\s+', ' ', content.lower().strip())
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:16]


def _read_file(path):
    """Read file content, return empty string if not exists."""
    if not os.path.exists(path):
        return ''
    with open(path, encoding='utf-8') as f:
        return f.read()


def _is_duplicate(existing_content, new_content):
    """Check if the new content is semantically a duplicate.

    Uses content hash comparison against all entries in the file.
    A match means the same information (ignoring whitespace/case) already exists.
    """
    new_hash = _content_hash(new_content)
    # Extract all existing entry content blocks
    entries = re.split(r'^---\s*$', existing_content, flags=re.MULTILINE)
    for entry in entries:
        # Get the content part (after metadata lines)
        lines = entry.strip().split('\n')
        content_lines = [l for l in lines
                         if not l.startswith('- **') and not l.startswith('###')]
        content = '\n'.join(content_lines).strip()
        if content and _content_hash(content) == new_hash:
            return True
    return False


def _generate_title(content):
    """Generate a short title from the content (first sentence or first line)."""
    first_line = content.strip().split('\n')[0].strip()
    # Remove markdown formatting
    first_line = re.sub(r'^[#*_\->\s]+', '', first_line)
    # Truncate
    if len(first_line) > 80:
        first_line = first_line[:77] + '...'
    return first_line or 'Untitled'

# ---------- public API ----------

def add_knowledge(content, category, workspace_name=None, tags=None, title=None,
                  source='manual', confidence='high'):
    """Add a knowledge entry to the appropriate category file.

    Args:
        content: the knowledge to store
        category: one of VALID_CATEGORIES
        workspace_name: workspace (default: active)
        tags: list of tag strings (without #)
        title: optional title (auto-generated if not provided)
        source: where this came from (manual, meeting, etc.)
        confidence: high, medium, low

    Returns:
        dict with result info, or None if duplicate
    """
    ctx = ws.get(workspace_name)
    workspace_name = ctx.name

    if category not in VALID_CATEGORIES:
        raise ValueError(f"Invalid category '{category}'. Must be one of: {VALID_CATEGORIES}")

    _ensure_dir(workspace_name)
    filepath = _category_file(workspace_name, category)
    existing = _read_file(filepath)

    # Dedupe check
    if _is_duplicate(existing, content):
        return None

    # Build entry
    if not title:
        title = _generate_title(content)

    date_str = datetime.now(WIB).strftime('%Y-%m-%d')
    tags_str = ', '.join(f'#{t.strip().lstrip("#")}' for t in (tags or []))

    entry_lines = []
    entry_lines.append(f'### {title}')
    entry_lines.append(f'- **Date**: {date_str}')
    if tags_str:
        entry_lines.append(f'- **Tags**: {tags_str}')
    entry_lines.append(f'- **Source**: {source}')
    entry_lines.append(f'- **Confidence**: {confidence}')
    entry_lines.append('')
    entry_lines.append(content.strip())
    entry_lines.append('')
    entry_lines.append('---')
    entry_lines.append('')

    entry = '\n'.join(entry_lines)

    # Initialize file with header if new
    if not existing.strip():
        header = f'# {category.title()} Knowledge\n\n'
        existing = header

    # Append
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(existing.rstrip() + '\n\n' + entry)

    return {
        'workspace': workspace_name,
        'category': category,
        'title': title,
        'tags': tags or [],
        'file': os.path.relpath(filepath, REPO_ROOT).replace('\\', '/'),
    }


def search_knowledge(query, workspace_name=None, category=None):
    """Search across all knowledge files for a query string.

    Returns list of matching entries with context.
    """
    ctx = ws.get(workspace_name)
    workspace_name = ctx.name
    knowledge_dir = _knowledge_dir(workspace_name)

    if not os.path.exists(knowledge_dir):
        return []

    results = []
    query_lower = query.lower()
    categories = [category] if category else VALID_CATEGORIES

    for cat in categories:
        filepath = _category_file(workspace_name, cat)
        if not os.path.exists(filepath):
            continue

        content = _read_file(filepath)
        entries = re.split(r'^---\s*$', content, flags=re.MULTILINE)

        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue
            # Skip the file header line only
            if entry.startswith('# ') and '###' not in entry:
                continue
            # Remove file header if it's at the top of the block
            if entry.startswith('# '):
                entry = re.sub(r'^# .+\n+', '', entry).strip()
            if not entry:
                continue
            if query_lower in entry.lower():
                # Extract title
                title_match = re.search(r'^### (.+)$', entry, re.MULTILINE)
                title = title_match.group(1) if title_match else 'Untitled'
                # Extract tags
                tags_match = re.search(r'\*\*Tags\*\*: (.+)$', entry, re.MULTILINE)
                tags = tags_match.group(1) if tags_match else ''
                # Extract date
                date_match = re.search(r'\*\*Date\*\*: (.+)$', entry, re.MULTILINE)
                date = date_match.group(1) if date_match else ''

                results.append({
                    'category': cat,
                    'title': title,
                    'tags': tags,
                    'date': date,
                    'content': entry,
                })

    return results


def list_entries(workspace_name=None, category=None):
    """List all entries in a category (or all categories)."""
    ctx = ws.get(workspace_name)
    workspace_name = ctx.name
    knowledge_dir = _knowledge_dir(workspace_name)

    if not os.path.exists(knowledge_dir):
        return []

    results = []
    categories = [category] if category else VALID_CATEGORIES

    for cat in categories:
        filepath = _category_file(workspace_name, cat)
        if not os.path.exists(filepath):
            continue

        content = _read_file(filepath)
        entries = re.split(r'^---\s*$', content, flags=re.MULTILINE)

        for entry in entries:
            entry = entry.strip()
            if not entry:
                continue
            if entry.startswith('# ') and '###' not in entry:
                continue
            if entry.startswith('# '):
                entry = re.sub(r'^# .+\n+', '', entry).strip()
            if not entry:
                continue
            title_match = re.search(r'^### (.+)$', entry, re.MULTILINE)
            title = title_match.group(1) if title_match else 'Untitled'
            date_match = re.search(r'\*\*Date\*\*: (.+)$', entry, re.MULTILINE)
            date = date_match.group(1) if date_match else ''
            results.append({
                'category': cat,
                'title': title,
                'date': date,
            })

    return results


def get_status(workspace_name=None):
    """Get entry counts per category."""
    ctx = ws.get(workspace_name)
    workspace_name = ctx.name
    knowledge_dir = _knowledge_dir(workspace_name)

    status = {}
    for cat in VALID_CATEGORIES:
        filepath = _category_file(workspace_name, cat)
        if not os.path.exists(filepath):
            status[cat] = 0
            continue
        content = _read_file(filepath)
        entries = re.split(r'^---\s*$', content, flags=re.MULTILINE)
        count = 0
        for e in entries:
            e = e.strip()
            if not e:
                continue
            if e.startswith('# ') and '###' not in e:
                continue
            if '###' in e:
                count += 1
        status[cat] = count

    return status

# ---------- CLI ----------

def cmd_add(args):
    ctx = ws.get(args.workspace)
    tags = [t.strip() for t in args.tags.split(',')] if args.tags else []

    result = add_knowledge(
        content=args.content,
        category=args.category,
        workspace_name=args.workspace,
        tags=tags,
        title=args.title,
        source=args.source,
        confidence=args.confidence,
    )

    if result:
        print(f"[{ctx.name}] Stored in {result['category']}:")
        print(f"  Title: {result['title']}")
        print(f"  File:  {result['file']}")
        if result['tags']:
            print(f"  Tags:  {', '.join('#' + t for t in result['tags'])}")
    else:
        print(f"[{ctx.name}] Duplicate detected - not stored.")


def cmd_search(args):
    ctx = ws.get(args.workspace)
    results = search_knowledge(args.query, workspace_name=args.workspace,
                               category=args.category)

    if not results:
        print(f"[{ctx.name}] No results for '{args.query}'.")
        return

    print(f"[{ctx.name}] Found {len(results)} result(s) for '{args.query}':\n")
    for r in results:
        print(f"  [{r['category']}] {r['title']} ({r['date']})")
        # Show snippet
        lines = r['content'].split('\n')
        content_lines = [l for l in lines
                         if not l.startswith('- **') and not l.startswith('###') and l.strip()]
        snippet = ' '.join(content_lines)[:150]
        print(f"    {snippet}")
        print()


def cmd_list(args):
    ctx = ws.get(args.workspace)
    entries = list_entries(workspace_name=args.workspace, category=args.category)

    if not entries:
        print(f"[{ctx.name}] No entries found.")
        return

    print(f"[{ctx.name}] Knowledge entries ({len(entries)}):\n")
    for e in entries:
        print(f"  [{e['category']:<15}] {e['title']} ({e['date']})")


def cmd_status(args):
    ctx = ws.get(args.workspace)
    status = get_status(workspace_name=args.workspace)
    total = sum(status.values())

    print(f"[{ctx.name}] Knowledge Store: {total} entries\n")
    for cat, count in sorted(status.items()):
        bar = '#' * count
        print(f"  {cat:<15} {count:>3}  {bar}")


def main():
    p = argparse.ArgumentParser(description='Knowledge Store')
    sub = p.add_subparsers(dest='cmd')

    # add
    ap = sub.add_parser('add', help='Add a knowledge entry')
    ap.add_argument('--workspace', default=None)
    ap.add_argument('--category', required=True, choices=VALID_CATEGORIES)
    ap.add_argument('--content', required=True, help='The knowledge to store')
    ap.add_argument('--title', default=None, help='Optional title')
    ap.add_argument('--tags', default='', help='Comma-separated tags')
    ap.add_argument('--source', default='manual')
    ap.add_argument('--confidence', default='high', choices=['high', 'medium', 'low'])

    # search
    sp = sub.add_parser('search', help='Search knowledge')
    sp.add_argument('--workspace', default=None)
    sp.add_argument('--query', required=True)
    sp.add_argument('--category', default=None, choices=VALID_CATEGORIES)

    # list
    lp = sub.add_parser('list', help='List entries')
    lp.add_argument('--workspace', default=None)
    lp.add_argument('--category', default=None, choices=VALID_CATEGORIES)

    # status
    stp = sub.add_parser('status', help='Show entry counts')
    stp.add_argument('--workspace', default=None)

    args = p.parse_args()

    handlers = {
        'add': cmd_add,
        'search': cmd_search,
        'list': cmd_list,
        'status': cmd_status,
    }

    handler = handlers.get(args.cmd)
    if handler:
        handler(args)
    else:
        p.print_help()


if __name__ == '__main__':
    main()
