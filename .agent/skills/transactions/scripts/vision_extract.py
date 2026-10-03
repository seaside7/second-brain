"""Vision extraction of transaction rows from screenshots (Gemini).

The Transactions upload flow lets the owner drop a bank/e-wallet app screenshot
instead of a PDF. Screenshots have no fixed layout, so rows are read back by a
multimodal model. This module calls the Gemini generateContent API directly
(no SDK, no secrets in code): the key is read from GEMINI_API_KEY or the
gemini-image skill's token.env at call time and is never logged or stored.

The model returns a strict JSON array of transaction rows; normalization to the
extracted-row contract happens in import_engine.upload_screenshot.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
_MODEL_CHAIN = ("gemini-2.5-flash", "gemini-2.5-pro")

_EXTRACT_PROMPT = """You are a transaction parser. This screenshot shows a bank or \
e-wallet transaction history. Extract every transaction line you can read.

Return ONLY a JSON array. Do not wrap it in markdown or add commentary.
Each object uses these keys:
- direction: "in" or "out"
- amount_rp: the whole amount in rupiah as an integer (ignore decimals/cents)
- description: the merchant/payee/notes text, exactly as shown
- merchant: the merchant or store name if identifiable, else ""
- recipient: the counterparty name for transfers, else ""
- occurred_at: "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM:SS" if a time is shown
- reference: the transaction reference/ID if visible, else ""

Rules:
- A negative amount or an outgoing marker means direction "out".
- Skip page headers, footers, totals, balances and account numbers.
- If the screenshot contains no transaction rows, return [].
"""


def _load_key() -> str:
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    tok = (Path(__file__).resolve().parent.parent.parent
           / "gemini-image" / "token.env")
    if tok.exists():
        for line in tok.read_text(encoding="utf-8").splitlines():
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


def _mime_for(path: Path) -> str:
    ext = path.suffix.lower()
    return {'.png': 'image/png', '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg', '.webp': 'image/webp'}.get(ext, 'image/png')


def extract_transactions(image_path: str | Path, **kw) -> list[dict]:
    """Return a list of raw transaction dicts read from a screenshot."""
    from urllib.error import HTTPError, URLError

    path = Path(image_path)
    b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    key = _load_key()
    if not key:
        raise RuntimeError(
            "Screenshot extraction needs a Gemini key (GEMINI_API_KEY or "
            "skills/gemini-image/token.env)")

    last_err = ""
    for model in _MODEL_CHAIN:
        url = f"{_BASE}/{model}:generateContent?key={key}"
        body = {
            "contents": [{"parts": [
                {"text": _EXTRACT_PROMPT},
                {"inlineData": {"mimeType": _mime_for(path), "data": b64}},
            ]}],
            "generationConfig": {"temperature": 0,
                                 "responseMimeType": "application/json",
                                 "maxOutputTokens": 4000},
        }
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.load(r)
            text = _parts_text(data)
            rows = _parse_json(text)
            if isinstance(rows, list) and len(rows) > 0:
                return rows
        except (HTTPError, URLError, json.JSONDecodeError, TimeoutError) as e:
            last_err = str(e)
        except RuntimeError:
            raise
    raise RuntimeError(f"Screenshot extraction failed: {last_err or 'no rows'}")


def _parts_text(data: dict) -> str:
    for cand in data.get("candidates", []):
        parts = ((cand.get("content") or {}).get("parts")) or []
        out = " ".join(p.get("text", "") for p in parts if "text" in p)
        if out.strip():
            return out
    return ""


def _parse_json(text: str) -> list:
    text = re.sub(r"^```(?:json)?", "", text.strip(), flags=re.IGNORECASE).strip()
    text = text.rstrip("`").strip()
    return json.loads(text)