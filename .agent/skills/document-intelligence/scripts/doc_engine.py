#!/usr/bin/env python3
"""
Document Intelligence Engine — Main orchestrator.

Syncs documents from Google Drive, indexes them, and provides
natural-language search and Q&A with source citations.

Usage:
    python doc_engine.py sync [--workspace X]
    python doc_engine.py search --query "API migration"
    python doc_engine.py ask --query "What does the contract say about SLA?"
    python doc_engine.py list
    python doc_engine.py show --file-id <id>
    python doc_engine.py stats
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent.parent.parent.parent
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(REPO_ROOT / ".agent" / "scripts"))
sys.path.insert(0, str(REPO_ROOT / ".agent" / "workspaces"))

import workspace_resolver as ws
from doc_connector import list_files, list_meeting_files, download_text, SUPPORTED_MIME_TYPES
from doc_parser import parse_document
from doc_store import (upsert_document, search_documents, list_documents,
                       get_document, update_sync_time, get_stats)

WIB = timezone(timedelta(hours=7))

try:
    if sys.stdout.encoding != 'utf-8':
        sys.stdout.reconfigure(encoding='utf-8')
    if sys.stderr.encoding != 'utf-8':
        sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass


def _get_config(workspace_name=None):
    ctx = ws.get(workspace_name)
    return ctx.config("documents"), ctx


def _meetings_config(ctx):
    """Scoped config for meeting transcripts: each workspace gets its OWN index
    so /samudera can only ever search samudera meetings (workspace-scoping
    rules), never the shared default index."""
    return {"source": "google_drive", "meetings": True,
            "index_path": f"journal/state/{ctx.name}_meetings_index.json"}


# ── Commands ──

def cmd_sync(args):
    """Sync documents from Google Drive into the local index."""
    cfg, ctx = _get_config(args.workspace)

    if args.meetings:
        # Meeting transcripts live in the PERSONAL drive; sync ONLY this
        # workspace's Meeting Transcripts/<client>/ folder into a dedicated,
        # workspace-scoped index. Downloads must use the PERSONAL token too
        # (the target workspace's token cannot see those files -> 404).
        cfg = _meetings_config(ctx)
        files, error = list_meeting_files(args.workspace)
        token_workspace = "personal"
        print(f"[{ctx.name}] Syncing meeting transcripts from PERSONAL drive...",
              file=sys.stderr)
    else:
        token_workspace = None
        if not cfg:
            print(f"[{ctx.name}] No documents.json config found.")
            print(f"Create: .agent/workspaces/{ctx.name}/documents.json")
            print(f'  {{"source": "google_drive", "folder_id": "YOUR_FOLDER_ID"}}')
            return
        print(f"[{ctx.name}] Syncing documents from Google Drive...", file=sys.stderr)
        files, error = list_files(args.workspace)

    if error:
        print(f"[ERROR] {error}")
        return

    if not files:
        print(f"[{ctx.name}] No supported documents found in configured folder.")
        return

    print(f"[{ctx.name}] Found {len(files)} files. Processing...", file=sys.stderr)

    created = 0
    updated = 0
    unchanged = 0
    failed = 0

    for f in files:
        file_id = f["id"]
        name = f["name"]
        mime_type = f["mimeType"]
        modified = f.get("modifiedTime", "")
        web_url = f.get("webViewLink", "")

        print(f"  [{name}] ", end="", file=sys.stderr)

        # Download content
        content, dl_error = download_text(file_id, mime_type, args.workspace,
                                          token_workspace=token_workspace)
        if dl_error:
            print(f"SKIP ({dl_error})", file=sys.stderr)
            failed += 1
            continue

        # Parse
        text, metadata, parse_error = parse_document(content, mime_type, name)
        if parse_error:
            print(f"SKIP ({parse_error})", file=sys.stderr)
            failed += 1
            continue

        if not text or not text.strip():
            print(f"SKIP (empty content)", file=sys.stderr)
            failed += 1
            continue

        # Store/index
        action, doc = upsert_document(
            file_id=file_id,
            name=name,
            mime_type=mime_type,
            modified_time=modified,
            web_url=web_url,
            text=text,
            metadata=metadata,
            config=cfg,
        )

        if action == "created":
            created += 1
            print(f"INDEXED ({metadata.get('word_count', '?')} words)", file=sys.stderr)
        elif action == "updated":
            updated += 1
            print(f"UPDATED", file=sys.stderr)
        else:
            unchanged += 1
            print(f"unchanged", file=sys.stderr)

    update_sync_time(cfg)

    print(f"\n[{ctx.name}] Sync complete:")
    print(f"  Created:   {created}")
    print(f"  Updated:   {updated}")
    print(f"  Unchanged: {unchanged}")
    print(f"  Failed:    {failed}")


def cmd_search(args):
    """Search indexed documents by keyword/phrase."""
    cfg, ctx = _get_config(args.workspace)
    if args.meetings:
        cfg = _meetings_config(ctx)
    results = search_documents(args.query, config=cfg, limit=args.limit)

    if not results:
        print(f"[{ctx.name}] No documents match: '{args.query}'")
        return

    print(f"[{ctx.name}] Found {len(results)} document(s) for '{args.query}':\n")
    for r in results:
        print(f"  [{r['relevance_score']:.0%}] {r['name']}")
        print(f"       {r['web_url']}")
        if r.get("snippet"):
            print(f"       \"{r['snippet'][:120]}\"")
        print()


def cmd_ask(args):
    """Answer a question using indexed documents + LLM."""
    cfg, ctx = _get_config(args.workspace)
    if args.meetings:
        cfg = _meetings_config(ctx)

    # 1. Search for relevant documents
    results = search_documents(args.query, config=cfg, limit=3)
    if not results:
        print(f"[{ctx.name}] No relevant documents found for: '{args.query}'")
        return

    # 2. Build context from top results
    context_parts = []
    sources = []
    for r in results:
        doc = get_document(r["file_id"], config=cfg)
        if doc and doc.get("text"):
            # Take first 2000 chars of each doc
            excerpt = doc["text"][:2000]
            context_parts.append(f"Document: {doc['name']}\nContent:\n{excerpt}\n")
            sources.append({"name": doc["name"], "url": doc.get("web_url", ""), "file_id": doc["file_id"]})

    if not context_parts:
        print(f"[{ctx.name}] Documents found but no text available.")
        return

    context = "\n---\n".join(context_parts)

    # 3. Ask LLM
    from deepseek_call import call as deepseek_call

    prompt = f"""Answer the following question based ONLY on the provided documents. If the answer is not in the documents, say "I could not find this in the available documents."

Always cite which document contains the answer.

Documents:
{context}

Question: {args.query}

Answer:"""

    print(f"[{ctx.name}] Searching {len(results)} document(s)...\n", file=sys.stderr)

    ok, answer, meta = deepseek_call(prompt, max_tokens=800, temperature=0.2)

    if ok and answer:
        print(f"Answer:\n  {answer}\n")
        print(f"Sources:")
        for s in sources:
            print(f"  - {s['name']}")
            if s.get("url"):
                print(f"    {s['url']}")
    else:
        print(f"[ERROR] LLM failed to generate an answer.")


def cmd_list(args):
    """List all indexed documents."""
    cfg, ctx = _get_config(args.workspace)
    docs = list_documents(config=cfg)

    if not docs:
        print(f"[{ctx.name}] No documents indexed yet. Run 'sync' first.")
        return

    print(f"[{ctx.name}] Indexed documents ({len(docs)}):\n")
    for d in docs:
        print(f"  {d['name']}")
        print(f"    Words: {d['word_count']}  Modified: {d['modified_time'][:10]}")
        if d.get("web_url"):
            print(f"    {d['web_url']}")
        print()


def cmd_show(args):
    """Show details of a specific document."""
    cfg, ctx = _get_config(args.workspace)
    doc = get_document(args.file_id, config=cfg)
    if not doc:
        print(f"Document not found: {args.file_id}")
        return

    print(f"Name:      {doc['name']}")
    print(f"File ID:   {doc['file_id']}")
    print(f"Type:      {doc['mime_type']}")
    print(f"Modified:  {doc.get('modified_time', '?')}")
    print(f"Words:     {doc.get('word_count', 0)}")
    print(f"Indexed:   {doc.get('indexed_at', '?')}")
    print(f"URL:       {doc.get('web_url', '')}")
    if doc.get("text"):
        print(f"\n--- Content (first 500 chars) ---\n{doc['text'][:500]}")


def cmd_stats(args):
    """Show index statistics."""
    cfg, ctx = _get_config(args.workspace)
    stats = get_stats(config=cfg)
    print(f"[{ctx.name}] Document Index Stats:")
    print(f"  Total documents: {stats['total_documents']}")
    print(f"  Total words:     {stats['total_words']}")
    print(f"  Last sync:       {stats['last_sync'] or 'never'}")


# ── CLI ──

def main():
    p = argparse.ArgumentParser(description="Document Intelligence Engine")
    p.add_argument("--workspace", default=None)
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("sync", help="Sync documents from Google Drive")
    sp.add_argument("--meetings", action="store_true",
                    help="sync ONLY this workspace's meeting transcripts from "
                         "the personal drive (Meeting Transcripts/<client>/) "
                         "into a dedicated workspace-scoped index")

    sp = sub.add_parser("search", help="Search indexed documents")
    sp.add_argument("--query", required=True)
    sp.add_argument("--limit", type=int, default=10)
    sp.add_argument("--meetings", action="store_true",
                    help="scope to this workspace's meeting transcripts "
                         "(Meeting Transcripts/<client>/ in the personal drive)")

    ap = sub.add_parser("ask", help="Ask a question about documents")
    ap.add_argument("--query", required=True)
    ap.add_argument("--meetings", action="store_true",
                    help="scope to this workspace's meeting transcripts "
                         "(Meeting Transcripts/<client>/ in the personal drive)")

    sub.add_parser("list", help="List indexed documents")

    shp = sub.add_parser("show", help="Show document details")
    shp.add_argument("--file-id", required=True, dest="file_id")

    sub.add_parser("stats", help="Show index statistics")

    args = p.parse_args()

    handlers = {
        "sync": cmd_sync,
        "search": cmd_search,
        "ask": cmd_ask,
        "list": cmd_list,
        "show": cmd_show,
        "stats": cmd_stats,
    }

    handler = handlers.get(args.cmd)
    if handler:
        handler(args)
    else:
        p.print_help()


if __name__ == "__main__":
    main()
