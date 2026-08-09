#!/usr/bin/env python3
"""
contacts.py — Minimal people/contacts store.

Resolves natural-language names to email addresses and other identifiers.
Workspace-scoped. Stores in .agent/workspaces/<workspace>/contacts.json.

Usage:
    from contacts import resolve_contact, add_contact, list_contacts

    email = resolve_contact("Marc")       # → "marc@example.com" or None
    add_contact("Marc", email="marc@example.com", role="PM")
    all_contacts = list_contacts()
"""
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / ".agent" / "workspaces"))
import workspace_resolver as ws


def _contacts_path(workspace_name=None):
    ctx = ws.get(workspace_name)
    return os.path.join(ctx.dir, "contacts.json")


def _load(workspace_name=None):
    path = _contacts_path(workspace_name)
    if not os.path.exists(path):
        return {"contacts": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save(data, workspace_name=None):
    path = _contacts_path(workspace_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def resolve_contact(name, workspace_name=None):
    """Resolve a name to a contact dict. Matches on name, nickname, or email prefix.
    Returns dict with at least {name, email} or None."""
    if not name:
        return None

    data = _load(workspace_name)
    name_lower = name.lower().strip()

    for contact in data.get("contacts", []):
        # Match on name
        if contact.get("name", "").lower() == name_lower:
            return contact
        # Match on nicknames
        for nick in contact.get("nicknames", []):
            if nick.lower() == name_lower:
                return contact
        # Match on email prefix
        email = contact.get("email", "")
        if email and email.split("@")[0].lower() == name_lower:
            return contact
        # Partial name match (first name)
        if name_lower in contact.get("name", "").lower():
            return contact

    return None


def add_contact(name, email="", role="", nicknames=None, workspace_name=None, **extra):
    """Add or update a contact."""
    data = _load(workspace_name)

    # Check if exists (update)
    for contact in data["contacts"]:
        if contact.get("name", "").lower() == name.lower() or contact.get("email", "") == email:
            contact["name"] = name
            if email:
                contact["email"] = email
            if role:
                contact["role"] = role
            if nicknames:
                contact["nicknames"] = list(set(contact.get("nicknames", []) + nicknames))
            contact.update(extra)
            _save(data, workspace_name)
            return contact

    # New contact
    contact = {"name": name, "email": email, "role": role, "nicknames": nicknames or []}
    contact.update(extra)
    data["contacts"].append(contact)
    _save(data, workspace_name)
    return contact


def list_contacts(workspace_name=None):
    """List all contacts."""
    return _load(workspace_name).get("contacts", [])


# CLI
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Contacts store")
    sub = p.add_subparsers(dest="cmd")

    ap = sub.add_parser("add")
    ap.add_argument("--name", required=True)
    ap.add_argument("--email", default="")
    ap.add_argument("--role", default="")
    ap.add_argument("--nicknames", default="", help="Comma-separated")

    sub.add_parser("list")

    rp = sub.add_parser("resolve")
    rp.add_argument("name")

    args = p.parse_args()

    if args.cmd == "add":
        nicks = [n.strip() for n in args.nicknames.split(",") if n.strip()] if args.nicknames else []
        c = add_contact(args.name, email=args.email, role=args.role, nicknames=nicks)
        print(f"Saved: {c}")
    elif args.cmd == "list":
        for c in list_contacts():
            nicks = f" ({', '.join(c.get('nicknames', []))})" if c.get("nicknames") else ""
            print(f"  {c['name']}{nicks} — {c.get('email', '')} [{c.get('role', '')}]")
    elif args.cmd == "resolve":
        result = resolve_contact(args.name)
        if result:
            print(f"Found: {result['name']} <{result.get('email', '')}>")
        else:
            print(f"Not found: {args.name}")
    else:
        p.print_help()
