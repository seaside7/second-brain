"""
Document Store — Local index of documents with full-text search.

Stores metadata + extracted text in a JSON file. Supports:
- Add/update documents (deduplicated by drive_file_id)
- Full-text keyword search
- Modified-time-based change detection
- Stable identifiers

Index path is configurable via workspace documents.json.
"""
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent.parent
DEFAULT_INDEX_PATH = str(REPO_ROOT / "journal" / "state" / "document_index.json")
WIB = timezone(timedelta(hours=7))


def _index_path(config=None):
    if config and config.get("index_path"):
        path = config["index_path"]
        if not os.path.isabs(path):
            path = str(REPO_ROOT / path)
        return path
    return DEFAULT_INDEX_PATH


def _load_index(config=None):
    path = _index_path(config)
    if not os.path.exists(path):
        return {"documents": {}, "last_sync": None}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_index(index, config=None):
    path = _index_path(config)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def upsert_document(file_id, name, mime_type, modified_time, web_url, text, metadata=None, config=None):
    """Add or update a document in the index. Deduplicates by file_id.
    Returns (action: 'created'|'updated'|'unchanged', doc)."""
    index = _load_index(config)
    docs = index.setdefault("documents", {})
    now = datetime.now(WIB).isoformat(timespec="seconds")

    existing = docs.get(file_id)

    if existing:
        # Check if modified
        if existing.get("modified_time") == modified_time:
            return "unchanged", existing
        # Update
        existing["name"] = name
        existing["mime_type"] = mime_type
        existing["modified_time"] = modified_time
        existing["web_url"] = web_url
        existing["text"] = text[:50000]  # Cap at 50k chars
        existing["word_count"] = len(text.split()) if text else 0
        existing["metadata"] = metadata or {}
        existing["indexed_at"] = now
        existing["text_hash"] = hashlib.sha256((text or "").encode()).hexdigest()[:16]
        _save_index(index, config)
        return "updated", existing
    else:
        # Create new
        doc = {
            "file_id": file_id,
            "name": name,
            "mime_type": mime_type,
            "modified_time": modified_time,
            "web_url": web_url,
            "text": text[:50000],
            "word_count": len(text.split()) if text else 0,
            "metadata": metadata or {},
            "indexed_at": now,
            "text_hash": hashlib.sha256((text or "").encode()).hexdigest()[:16],
        }
        docs[file_id] = doc
        _save_index(index, config)
        return "created", doc


def get_document(file_id, config=None):
    """Get a document by file_id. Returns doc dict or None."""
    index = _load_index(config)
    return index.get("documents", {}).get(file_id)


def list_documents(config=None):
    """List all indexed documents (without full text)."""
    index = _load_index(config)
    docs = []
    for doc in index.get("documents", {}).values():
        docs.append({
            "file_id": doc["file_id"],
            "name": doc["name"],
            "mime_type": doc["mime_type"],
            "modified_time": doc.get("modified_time", ""),
            "word_count": doc.get("word_count", 0),
            "indexed_at": doc.get("indexed_at", ""),
            "web_url": doc.get("web_url", ""),
        })
    return sorted(docs, key=lambda d: d.get("modified_time", ""), reverse=True)


def search_documents(query, config=None, limit=10):
    """Full-text keyword search across all indexed documents.
    Returns list of {file_id, name, web_url, snippet, relevance_score}."""
    index = _load_index(config)
    query_lower = query.lower()
    query_words = set(query_lower.split())

    results = []
    for doc in index.get("documents", {}).values():
        text = (doc.get("text") or "").lower()
        name = (doc.get("name") or "").lower()

        # Score: how many query words appear in the document
        matches = sum(1 for w in query_words if w in text or w in name)
        if matches == 0:
            continue

        # Relevance = fraction of query words matched
        score = matches / len(query_words) if query_words else 0

        # Extract snippet around first match
        snippet = _extract_snippet(doc.get("text", ""), query_words)

        results.append({
            "file_id": doc["file_id"],
            "name": doc["name"],
            "web_url": doc.get("web_url", ""),
            "snippet": snippet,
            "relevance_score": round(score, 2),
            "word_count": doc.get("word_count", 0),
        })

    results.sort(key=lambda r: r["relevance_score"], reverse=True)
    return results[:limit]


def _extract_snippet(text, query_words, window=150):
    """Extract a text snippet around the first query word match."""
    text_lower = text.lower()
    for word in query_words:
        idx = text_lower.find(word)
        if idx >= 0:
            start = max(0, idx - window // 2)
            end = min(len(text), idx + window)
            snippet = text[start:end].strip()
            if start > 0:
                snippet = "..." + snippet
            if end < len(text):
                snippet = snippet + "..."
            return snippet
    return text[:window] + "..." if len(text) > window else text


def update_sync_time(config=None):
    """Update the last sync timestamp."""
    index = _load_index(config)
    index["last_sync"] = datetime.now(WIB).isoformat(timespec="seconds")
    _save_index(index, config)


def get_stats(config=None):
    """Get index statistics."""
    index = _load_index(config)
    docs = index.get("documents", {})
    return {
        "total_documents": len(docs),
        "total_words": sum(d.get("word_count", 0) for d in docs.values()),
        "last_sync": index.get("last_sync"),
    }
