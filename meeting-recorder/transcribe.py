#!/usr/bin/env python3
"""Transcription engine chain for the local meeting note-taker.

Chain (engine=auto): OpenAI Whisper (whisper-1, $0.006/min, the owner's key in
root .env) -> Gemini API (audio-in, speaker labels) -> whisper.cpp on GPU as
fallback. There is NO automatic CPU fallback: the owner's rule. engine=cpu
(explicit only) shells out to the legacy faster-whisper script.

Usage:
  python3 transcribe.py --in recording.wav --out transcript.md \
      [--engine auto|openai|whispercpp|cli|cpu] [--lang auto|en|id]

Output: markdown transcript with **[mm:ss]** timestamps (same format the /mom
pipeline already consumes) + a plain .txt sibling.
"""
import argparse
import base64
import datetime
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid

from common import REPO_ROOT, fmt_ts, load_config, load_gemini_key, load_openai_key

LOG_PATH = os.path.join(REPO_ROOT, "dashboard-data", "meeting_recorder_log.jsonl")
GEMINI_BASE = "https://generativelanguage.googleapis.com"
# Gemini audio pricing is folded into normal token pricing; log tokens + est cost.
GEMINI_PRICE_PER_MTOK = {"in": 0.30, "out": 2.50}  # flash-tier list price, USD
# OpenAI Whisper audio transcription list price, USD per minute of audio.
OPENAI_WHISPER_PRICE_PER_MIN = 0.006
OPENAI_TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"

GEMINI_PROMPT = """Transcribe this meeting recording completely and accurately.
The audio may mix English and Indonesian; transcribe each utterance in its
original language, do not translate.

Output format, one line per utterance, nothing else:
**[mm:ss]** Speaker N: text

Rules:
- Timestamps are elapsed time from the start of the audio.
- Distinguish speakers by voice; label them Speaker 1, Speaker 2, ... consistently.
  If a speaker states their own name or is addressed by name, use that name instead.
- Do not summarize, skip, or clean up content. Include the full transcript.
"""

class EngineSkip(Exception):
    """This engine is unavailable/failed; try the next one in the chain."""

def log_row(row):
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
    row["ts_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")

def audio_duration(path, ffmpeg):
    ffprobe = os.path.join(os.path.dirname(ffmpeg), "ffprobe") if os.sep in ffmpeg else "ffprobe"
    try:
        out = subprocess.run([ffprobe, "-v", "quiet", "-show_entries",
                              "format=duration", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out)
    except Exception:
        return 0.0

# ---------- engine: whisper.cpp (GPU only) ----------

def _winpath(p):
    """WSL path -> Windows path for args passed to a Windows .exe via interop."""
    return subprocess.run(["wslpath", "-w", p], capture_output=True,
                          text=True, check=True).stdout.strip()

def run_whispercpp(audio, cfg, lang):
    machine = cfg["machine"]
    bin_path = machine.get("whispercpp_bin") or ""
    model = machine.get("whispercpp_model") or ""
    if not bin_path or not model or not os.path.exists(model):
        raise EngineSkip("whisper.cpp binary/model not configured on this machine")

    # A Windows .exe invoked from WSL can't read WSL-only paths (/tmp): keep the
    # temp files on a Windows drive and pass Windows-style path arguments.
    win_interop = bin_path.lower().endswith(".exe")
    tmp_parent = os.path.dirname(bin_path) if win_interop else None

    ffmpeg = machine.get("ffmpeg", "ffmpeg")
    with tempfile.TemporaryDirectory(dir=tmp_parent) as td:
        wav16 = os.path.join(td, "audio16k.wav")
        subprocess.run([ffmpeg, "-y", "-v", "quiet", "-i", audio,
                        "-ac", "1", "-ar", "16000", wav16], check=True, timeout=600)
        prefix = os.path.join(td, "out")
        if win_interop:
            cmd = [bin_path, "-m", _winpath(model), "-f", _winpath(wav16),
                   "-oj", "-of", _winpath(prefix)]
        else:
            cmd = [bin_path, "-m", model, "-f", wav16, "-oj", "-of", prefix]
        if lang != "auto":
            cmd += ["-l", lang]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=3 * 3600)
        if r.returncode != 0:
            raise EngineSkip(f"whisper.cpp failed: {r.stderr[-300:]}")
        gpu_markers = ("Metal", "Vulkan", "CUDA", "gpu device")
        used_gpu = any(m.lower() in (r.stderr + r.stdout).lower() for m in gpu_markers)
        if cfg.get("require_gpu", True) and not used_gpu:
            raise EngineSkip("whisper.cpp ran without GPU (require_gpu on) -> skipping to CLI")
        with open(prefix + ".json", encoding="utf-8") as f:
            data = json.load(f)

    lines = []
    for seg in data.get("transcription", []):
        text = seg.get("text", "").strip()
        if not text:
            continue
        start_s = seg.get("offsets", {}).get("from", 0) / 1000.0
        lines.append(f"**[{fmt_ts(start_s)}]** {text}")
    if not lines:
        raise EngineSkip("whisper.cpp produced an empty transcript")
    return lines, f"whisper.cpp `{os.path.basename(model)}` (GPU)"

# ---------- engine: cli (Gemini API, audio-in) ----------

def _gemini_req(url, body, key, timeout=1800):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)

def _gemini_upload_file(path, mime, key):
    """Files API resumable upload; returns the file URI once ACTIVE."""
    size = os.path.getsize(path)
    start = urllib.request.Request(
        f"{GEMINI_BASE}/upload/v1beta/files",
        data=json.dumps({"file": {"display_name": os.path.basename(path)}}).encode(),
        headers={"x-goog-api-key": key,
                 "X-Goog-Upload-Protocol": "resumable",
                 "X-Goog-Upload-Command": "start",
                 "X-Goog-Upload-Header-Content-Length": str(size),
                 "X-Goog-Upload-Header-Content-Type": mime,
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(start, timeout=120) as r:
        upload_url = r.headers["X-Goog-Upload-URL"]
    with open(path, "rb") as f:
        blob = f.read()
    up = urllib.request.Request(
        upload_url, data=blob,
        headers={"X-Goog-Upload-Command": "upload, finalize",
                 "X-Goog-Upload-Offset": "0",
                 "Content-Length": str(size)})
    with urllib.request.urlopen(up, timeout=1800) as r:
        info = json.load(r)["file"]
    # wait until processed
    for _ in range(60):
        if info.get("state") == "ACTIVE":
            return info["uri"]
        time.sleep(5)
        req = urllib.request.Request(f"{GEMINI_BASE}/v1beta/{info['name']}",
                                     headers={"x-goog-api-key": key})
        with urllib.request.urlopen(req, timeout=60) as r:
            info = json.load(r)
    raise EngineSkip(f"Gemini file stuck in state {info.get('state')}")

def run_gemini(audio, cfg, lang):
    key = load_gemini_key()
    machine = cfg["machine"]
    ffmpeg = machine.get("ffmpeg", "ffmpeg")
    model = cfg.get("gemini_model", "gemini-2.5-flash")

    with tempfile.TemporaryDirectory() as td:
        # compress to ogg/opus 16k mono: ~1 MB per 8 min, keeps requests small
        ogg = os.path.join(td, "audio.ogg")
        subprocess.run([ffmpeg, "-y", "-v", "quiet", "-i", audio, "-ac", "1",
                        "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", ogg],
                       check=True, timeout=600)
        size = os.path.getsize(ogg)
        prompt = GEMINI_PROMPT
        if lang != "auto":
            prompt += f"\nThe meeting is primarily in '{lang}'."
        if size < 15 * 1024 * 1024:  # inline under the ~20MB request cap
            audio_part = {"inline_data": {
                "mime_type": "audio/ogg",
                "data": base64.b64encode(open(ogg, "rb").read()).decode()}}
        else:
            uri = _gemini_upload_file(ogg, "audio/ogg", key)
            audio_part = {"file_data": {"mime_type": "audio/ogg", "file_uri": uri}}

        body = {"contents": [{"parts": [audio_part, {"text": prompt}]}],
                "generationConfig": {"temperature": 0.1, "maxOutputTokens": 65536}}
        try:
            data = _gemini_req(f"{GEMINI_BASE}/v1beta/models/{model}:generateContent",
                               body, key)
        except urllib.error.HTTPError as e:
            raise EngineSkip(f"Gemini HTTP {e.code}: {e.read().decode()[:300]}")

    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        raise EngineSkip(f"Gemini returned no text: {json.dumps(data)[:300]}")

    usage = data.get("usageMetadata", {})
    in_tok = usage.get("promptTokenCount", 0)
    out_tok = usage.get("candidatesTokenCount", 0)
    cost = (in_tok * GEMINI_PRICE_PER_MTOK["in"] +
            out_tok * GEMINI_PRICE_PER_MTOK["out"]) / 1e6
    log_row({"kind": "transcribe", "engine": f"gemini:{model}",
             "file": os.path.basename(audio), "in_tok": in_tok,
             "out_tok": out_tok, "est_usd": round(cost, 4)})

    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise EngineSkip("Gemini transcript empty")
    return lines, f"Gemini `{model}` (audio-in, speaker labels, ~${cost:.3f})"

# ---------- engine: openai (Whisper API, cheapest) ----------

def _multipart(fields, file_field, filename, data, content_type):
    """Build a multipart/form-data body without pulling in a requests dep."""
    boundary = "----psb" + uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\nContent-Type: {content_type}\r\n\r\n'.encode())
    parts.append(data)
    parts.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(parts), boundary

def run_openai_whisper(audio, cfg, lang):
    key = load_openai_key()
    machine = cfg["machine"]
    ffmpeg = machine.get("ffmpeg", "ffmpeg")
    model = cfg.get("openai_transcribe_model",
                    os.environ.get("OPENAI_TRANSCRIBE_MODEL", "whisper-1"))

    with tempfile.TemporaryDirectory() as td:
        ogg = os.path.join(td, "audio.ogg")
        subprocess.run([ffmpeg, "-y", "-v", "quiet", "-i", audio, "-ac", "1",
                        "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", ogg],
                       check=True, timeout=600)
        fields = {"model": model, "response_format": "verbose_json"}
        if lang != "auto":
            fields["language"] = lang
        body, boundary = _multipart(fields, "file", os.path.basename(audio),
                                    open(ogg, "rb").read(), "audio/ogg")
        req = urllib.request.Request(
            OPENAI_TRANSCRIBE_URL, data=body,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": f"multipart/form-data; boundary={boundary}"})
        try:
            with urllib.request.urlopen(req, timeout=1800) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            raise EngineSkip(f"OpenAI HTTP {e.code}: {e.read().decode()[:300]}")

    dur = audio_duration(audio, ffmpeg)
    cost = dur / 60.0 * OPENAI_WHISPER_PRICE_PER_MIN
    log_row({"kind": "transcribe", "engine": f"openai:{model}",
             "file": os.path.basename(audio),
             "est_usd": round(cost, 4)})

    lines = []
    for seg in data.get("segments", []):
        text = seg.get("text", "").strip()
        if not text:
            continue
        lines.append(f"**[{fmt_ts(seg.get('start', 0))}]** {text}")
    if not lines:
        raise EngineSkip("OpenAI whisper returned no text")
    return lines, f"OpenAI `{model}` (~${cost:.3f})"

# ---------- engine: cpu (explicit only, legacy faster-whisper) ----------

def run_cpu(audio, cfg, lang, out_md):
    venv_py = os.path.expanduser("~/.venvs/whisper/bin/python")
    script = os.path.join(REPO_ROOT, "scripts", "transcribe_audio.py")
    if not os.path.exists(venv_py):
        raise EngineSkip("whisper venv missing (~/.venvs/whisper)")
    subprocess.run([venv_py, script, "--in", audio, "--out", out_md,
                    "--model", "small", "--lang", lang], check=True)
    return None, "faster-whisper small (cpu, explicit)"

# ---------- orchestration ----------

def transcribe(audio, out_md, engine=None, lang=None, cfg=None):
    """Returns (out_md, engine_note). Raises RuntimeError if all engines fail."""
    cfg = cfg or load_config()
    engine = engine or cfg.get("engine", "auto")
    lang = lang or cfg.get("language", "auto")

    # auto prefers OpenAI Whisper (cheapest, the owner's key), then Gemini for
    # speaker labels, then whisper.cpp on GPU. NEVER falls back to CPU.
    chain = {"auto": ["openai", "cli", "whispercpp"],
             "whispercpp": ["whispercpp"],
             "openai": ["openai"],
             "cli": ["cli"],
             "cpu": ["cpu"]}[engine]

    errors = []
    for eng in chain:
        try:
            print(f"[transcribe] trying engine: {eng}", flush=True)
            if eng == "cpu":
                run_cpu(audio, cfg, lang, out_md)
                return out_md, "faster-whisper (cpu, explicit)"
            fns = {"whispercpp": run_whispercpp, "cli": run_gemini,
                   "openai": run_openai_whisper}
            lines, note = fns[eng](audio, cfg, lang)
            dur = audio_duration(audio, cfg["machine"].get("ffmpeg", "ffmpeg"))
            header = (f"# Transcript: {os.path.basename(audio)}\n\n"
                      f"- Engine: {note}\n"
                      f"- Audio duration: {fmt_ts(dur)}\n"
                      f"- Generated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n---\n\n")
            os.makedirs(os.path.dirname(os.path.abspath(out_md)) or ".", exist_ok=True)
            with open(out_md, "w", encoding="utf-8") as f:
                f.write(header + "\n".join(lines) + "\n")
            txt = os.path.splitext(out_md)[0] + ".txt"
            with open(txt, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            print(f"[transcribe] OK via {eng} -> {out_md}", flush=True)
            return out_md, note
        except EngineSkip as e:
            print(f"[transcribe] {eng} skipped: {e}", file=sys.stderr, flush=True)
            errors.append(f"{eng}: {e}")
        except (subprocess.SubprocessError, OSError) as e:
            print(f"[transcribe] {eng} error: {e}", file=sys.stderr, flush=True)
            errors.append(f"{eng}: {e}")
    raise RuntimeError("all engines failed: " + " | ".join(errors))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", dest="out", required=True)
    ap.add_argument("--engine", choices=["auto", "openai", "whispercpp", "cli", "cpu"])
    ap.add_argument("--lang", choices=["auto", "en", "id"])
    args = ap.parse_args()
    if not os.path.isfile(args.inp):
        sys.exit(f"ERROR: input not found: {args.inp}")
    out, note = transcribe(args.inp, args.out, args.engine, args.lang)
    print(f"DONE: {out} ({note})")

if __name__ == "__main__":
    main()
