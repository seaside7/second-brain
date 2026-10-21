"""Telegram dev bot: pick a project, pull the latest, chat to develop, get screenshots (FE) or test results (BE).

A thin front end over the dashboard Coding Agent API (``dashboard/coding_agent.py``, localhost:3737). It does not
contain a coding engine: prompts go to the repo's persistent OpenCode session. Safety (see docs/TELEGRAM_DEV_BOT.md):
- hard deny-list (trading-brain, second-brain) enforced in code, never removable through ``.env``;
- only the owner's chat is answered; no secrets in any outgoing text;
- nothing is committed or pushed without a confirmation tap, never to main/master/develop, never with force.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets as _secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT / '.env'
STATE_PATH = ROOT / 'journal' / 'state' / 'devbot_state.json'
HARD_DENY = frozenset({'trading-brain', 'second-brain'})        # live trading runs from these; never touchable
NEVER_PUSH = frozenset({'main', 'master', 'develop'})
BACKEND_TYPES = {'be', 'api', 'backend', 'cms', 'php', 'worker'}   # same set as the dashboard
SECRET_FILE_RE = re.compile(r'(^|/)(\.env($|\.)|id_rsa|id_ed25519|credentials\.json|token\.json|.*\.(pem|key|p12|pfx))', re.I)
JUNK_RE = re.compile(r'(^|/)(node_modules|dist|build|\.next|\.nuxt|vendor|__pycache__|\.cache|coverage)/|\.log$')
GIT_NAME = os.environ.get('CODING_GIT_NAME', 'Second Brain Coding Agent')
GIT_EMAIL = os.environ.get('CODING_GIT_EMAIL', 'said@catalyze.id')
MAX_TG = 3900


class DevBotError(Exception):
    pass


def load_env(path: Path) -> dict[str, str]:
    """Only the bot's own keys are read from .env (never the other secrets in the file)."""
    out: dict[str, str] = {}
    try:
        for line in Path(path).read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            key = key.strip()
            if key.startswith('DEV_') or key.startswith('DEVBOT_'):
                out[key] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def redact(text: str, secret_values: list[str]) -> str:
    for value in secret_values:
        if value and len(value) >= 6:
            text = text.replace(value, '[REDACTED]')
    text = re.sub(r'(https?://)[^/\s:@]+:[^/\s@]+@', r'\1***@', text)       # user:pass@host
    text = re.sub(r'(https?://)[^/\s@]+@', r'\1***@', text)                 # token@host
    return text


def chunks(text: str, size: int = MAX_TG) -> list[str]:
    text = text or ''
    return [text[i:i + size] for i in range(0, len(text), size)] or ['']


def slug(text: str, n: int = 40) -> str:
    s = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')[:n].strip('-')
    return s or 'update'


class DashboardApi:
    def __init__(self, base: str, session: Any = requests):
        self.base = base.rstrip('/')
        self.session = session

    def _call(self, method: str, path: str, body: dict | None = None, timeout: int = 60) -> Any:
        try:
            r = self.session.request(method, self.base + path, json=body, timeout=timeout)
        except Exception as exc:
            raise DevBotError(f'dashboard tidak bisa dihubungi ({type(exc).__name__})')
        try:
            data = r.json()
        except Exception:
            data = {}
        if r.status_code >= 400:
            raise DevBotError((data or {}).get('error') or f'dashboard error {r.status_code}')
        return data

    def repos(self) -> list[dict[str, Any]]:
        data = self._call('GET', '/api/coding/repos')
        return data if isinstance(data, list) else (data or {}).get('repos', [])

    def repo(self, name: str) -> dict[str, Any]:
        return self._call('GET', f'/api/coding/repos/{name}')

    def pull(self, name: str) -> dict[str, Any]:
        return self._call('POST', f'/api/coding/repos/{name}/pull', {}, timeout=700)

    def session(self, name: str) -> dict[str, Any]:
        return self._call('GET', f'/api/coding/repos/{name}/session')

    def message(self, name: str, text: str, mode: str | None = None) -> dict[str, Any]:
        return self._call('POST', f'/api/coding/repos/{name}/session/message', {'text': text, 'mode': mode})

    def permission(self, name: str, permission_id: str, response: str) -> dict[str, Any]:
        return self._call('POST', f'/api/coding/repos/{name}/permission', {'permission_id': permission_id, 'response': response})

    def stop(self, name: str) -> dict[str, Any]:
        return self._call('POST', f'/api/coding/repos/{name}/session/stop', {})

    def reset(self, name: str) -> dict[str, Any]:
        return self._call('POST', f'/api/coding/repos/{name}/session/reset', {})

    def preview_start(self, name: str) -> dict[str, Any]:
        return self._call('POST', f'/api/coding/repos/{name}/preview/start', {})

    def preview_status(self, name: str) -> dict[str, Any]:
        return self._call('GET', f'/api/coding/repos/{name}/preview/status')

    def preview_stop(self, name: str) -> dict[str, Any]:
        return self._call('POST', f'/api/coding/repos/{name}/preview/stop', {})


class TelegramHTTP:
    def __init__(self, token: str, session: Any = requests):
        self._token = token
        self._session = session

    def call(self, method: str, payload: dict[str, Any], timeout: int = 40) -> Any:
        r = self._session.post(f'https://api.telegram.org/bot{self._token}/{method}', json=payload, timeout=timeout)
        r.raise_for_status()
        return r.json().get('result')

    def updates(self, offset: int | None) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {'timeout': 30}
        if offset is not None:
            payload['offset'] = offset
        return self.call('getUpdates', payload, timeout=45) or []

    def send(self, chat_id: Any, text: str, keyboard: list | None = None) -> None:
        payload: dict[str, Any] = {'chat_id': chat_id, 'text': text}
        if keyboard:
            payload['reply_markup'] = {'inline_keyboard': keyboard}
        self.call('sendMessage', payload)

    def photo(self, chat_id: Any, data: bytes, caption: str = '') -> None:
        r = self._session.post(f'https://api.telegram.org/bot{self._token}/sendPhoto',
                               data={'chat_id': chat_id, 'caption': caption[:900]}, files={'photo': ('screenshot.png', data)}, timeout=60)
        r.raise_for_status()

    def document(self, chat_id: Any, name: str, data: bytes, caption: str = '') -> None:
        r = self._session.post(f'https://api.telegram.org/bot{self._token}/sendDocument',
                               data={'chat_id': chat_id, 'caption': caption[:900]}, files={'document': (name, data)}, timeout=60)
        r.raise_for_status()

    def answer(self, callback_id: str) -> None:
        self.call('answerCallbackQuery', {'callback_query_id': callback_id})


def run_git(path: str, *args: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(['git', '-C', path, *args], capture_output=True, text=True, timeout=timeout)


def run_shell(path: str, command: str, timeout: int = 600) -> tuple[int, str]:
    try:
        r = subprocess.run(command, shell=True, cwd=path, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or '') + (r.stderr or '')
    except subprocess.TimeoutExpired:
        return 124, f'timeout setelah {timeout} detik'


class DevBot:
    def __init__(self, *, env_path: Path = ENV_PATH, state_path: Path = STATE_PATH, api: Any = None, telegram: Any = None,
                 git: Any = None, shell: Any = None, shot: Any = None, now_fn: Any = None, sleep_fn: Any = time.sleep,
                 dry_run: bool = False):
        self.env = load_env(env_path)
        self.state_path = Path(state_path)
        self.chat_id = str(self.env.get('DEV_TELEGRAM_CHAT_ID') or '').strip()
        self.api = api or DashboardApi(self.env.get('DEVBOT_DASHBOARD_URL') or 'http://127.0.0.1:3737')
        self.telegram = telegram or TelegramHTTP(self.env.get('DEV_TELEGRAM_BOT_TOKEN', ''))
        self.git = git or run_git
        self.shell = shell or run_shell
        self.shot = shot
        self._now = now_fn or time.time
        self._sleep = sleep_fn
        self.dry_run = dry_run
        self.timeout_s = float(self.env.get('DEVBOT_JOB_TIMEOUT_MIN') or 45) * 60
        self._lock = threading.RLock()                        # the poll thread and the update loop share one state file
        deny = {x.strip() for x in str(self.env.get('DEVBOT_REPO_DENYLIST') or '').split(',') if x.strip()}
        self.denylist = set(HARD_DENY) | deny                       # the hard part can never be removed from .env
        self.allowlist = {x.strip() for x in str(self.env.get('DEVBOT_REPO_ALLOWLIST') or '').split(',') if x.strip()}
        self._secret_values = [self.env.get('DEV_TELEGRAM_BOT_TOKEN', '')]

    # ---------- plumbing ----------
    def _state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            return {'project': None, 'job': None, 'cards': {}, 'last_instruction': ''}

    def _save(self, state: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, self.state_path)

    def authorized(self, chat_id: Any) -> bool:
        return bool(self.chat_id) and str(chat_id) == self.chat_id

    def say(self, text: str, keyboard: list | None = None) -> str:
        text = redact(text, self._secret_values)
        parts = chunks(text)
        if self.dry_run:
            print(text)
        else:
            for i, part in enumerate(parts):
                self.telegram.send(self.chat_id, part, keyboard if i == len(parts) - 1 else None)
        return text

    def allowed(self, name: str) -> bool:
        if not name or name in self.denylist:
            return False
        return not self.allowlist or name in self.allowlist

    def list_repos(self) -> list[dict[str, Any]]:
        return [r for r in self.api.repos() if self.allowed(r.get('name', ''))]

    @staticmethod
    def is_backend(info: dict[str, Any]) -> bool:
        cfg = info.get('config') or {}
        return str(cfg.get('type') or '').lower() in BACKEND_TYPES or not cfg.get('previewEnabled', True)

    # ---------- commands ----------
    HELP = ('Perintah:\n/proyek - pilih project\n/mulai <repo> - pilih project\n/status - status project dan job\n'
            '/diff - file yang berubah\n/preview - screenshot (project FE)\n/test - jalankan test (project BE)\n'
            '/setujui [pesan commit] - commit dan push (pakai tombol konfirmasi)\n/batal - hentikan prompt yang berjalan\n'
            '/reset - lupakan sesi AI project ini\n/tutup - tinggalkan project\n/help - bantuan\n\n'
            'Teks biasa = instruksi untuk project yang dipilih. Awali dengan "plan dulu" kalau cuma mau rencana tanpa mengubah kode.')

    def process_update(self, update: dict[str, Any]) -> None:
        with self._lock:
            self._process(update)

    def _process(self, update: dict[str, Any]) -> None:
        callback = update.get('callback_query')
        if callback:
            chat = ((callback.get('message') or {}).get('chat') or {}).get('id')
            if not self.authorized(chat):
                return
            if callback.get('id') and not self.dry_run:
                try:
                    self.telegram.answer(callback['id'])
                except Exception:
                    pass
            self.callback(str(callback.get('data') or ''))
            return
        message = update.get('message') or {}
        text = message.get('text')
        if text is None or not self.authorized((message.get('chat') or {}).get('id')):
            return
        if text.startswith('/'):
            self.command(text)
        else:
            self.instruction(text)

    def command(self, text: str) -> None:
        parts = text.strip().split(None, 1)
        cmd = parts[0].lower().split('@')[0]
        arg = parts[1].strip() if len(parts) > 1 else ''
        try:
            if cmd in ('/start', '/help'):
                self.say(self.HELP)
            elif cmd == '/proyek':
                self.cmd_projects()
            elif cmd == '/mulai':
                self.select(arg) if arg else self.cmd_projects()
            elif cmd == '/status':
                self.cmd_status()
            elif cmd == '/diff':
                self.cmd_diff()
            elif cmd == '/preview':
                self.cmd_preview()
            elif cmd == '/test':
                self.cmd_test()
            elif cmd == '/setujui':
                self.cmd_approve(arg)
            elif cmd == '/batal':
                self.cmd_cancel()
            elif cmd == '/reset':
                self.cmd_reset()
            elif cmd == '/tutup':
                self.cmd_close()
            else:
                self.say('Perintah tidak dikenal. Ketik /help.')
        except DevBotError as exc:
            self.say(f'Gagal: {exc}')

    def cmd_projects(self) -> None:
        repos = self.list_repos()
        if not repos:
            self.say('Tidak ada project yang diizinkan.')
            return
        rows, row = [], []
        for r in repos:
            kind = 'BE' if self.is_backend(r) else 'FE'
            label = f"{r['name']} ({kind}){' *' if r.get('dirty') else ''}"
            row.append({'text': label, 'callback_data': f"dv:pick:{r['name']}"})
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        self.say('Pilih project (* = ada perubahan lokal belum di-commit):', rows)

    def _project(self) -> str:
        name = self._state().get('project')
        if not name or not self.allowed(name):
            raise DevBotError('belum ada project. Ketik /proyek untuk memilih.')
        return name

    # ---------- select = pull + briefing ----------
    def select(self, name: str, force_stale: bool = False) -> None:
        if not self.allowed(name):
            self.say(f'Project "{name}" tidak tersedia.')
            return
        info = self.api.repo(name)
        pull_ok, pull_note = True, ''
        if not force_stale:
            try:
                res = self.api.pull(name)
                pull_ok = bool(res.get('ok'))
                pull_note = (res.get('output') or '').strip()[-300:]
                info = res.get('repo') or info
            except DevBotError as exc:
                pull_ok, pull_note = False, str(exc)
        if not pull_ok:
            self.say(f'Pull {name} GAGAL: {pull_note or "tidak ada detail"}\nSaya tidak lanjut diam-diam di atas kode lama.',
                     [[{'text': 'Lanjut tanpa pull', 'callback_data': f'dv:stale:{name}'},
                       {'text': 'Batal', 'callback_data': 'dv:cancel'}]])
            return
        state = self._state()
        state['project'] = name
        self._save(state)
        self.say(self.briefing(name, info, pull_note, force_stale))

    def briefing(self, name: str, info: dict[str, Any], pull_note: str = '', stale: bool = False) -> str:
        path = info.get('path') or ''
        lines = [f'PROJECT {name}' + (' (TANPA PULL, kode bisa usang)' if stale else '')]
        kind = 'BE' if self.is_backend(info) else 'FE'
        cfg = info.get('config') or {}
        lines.append(f"Jenis: {kind} ({cfg.get('type') or '?'}) | branch {info.get('current_branch')} | remote {info.get('origin_url') or '-'}")
        if path:
            def g(*a: str) -> str:
                try:
                    r = self.git(path, *a)
                    return (r.stdout or '').strip() if r.returncode == 0 else ''
                except Exception:
                    return ''
            head = g('rev-parse', '--short', 'HEAD')
            counts = g('rev-list', '--left-right', '--count', '@{u}...HEAD')
            if head:
                lines.append(f'HEAD {head}' + (f' | tertinggal/di depan remote: {counts.replace(chr(9), "/")}' if counts else ''))
            log = g('log', '-5', '--pretty=%h %s (%an, %ar)')
            if log:
                lines.append('5 commit terakhir:\n' + '\n'.join('- ' + x for x in log.splitlines()))
            dirty = [x[3:] for x in g('status', '--porcelain').splitlines() if len(x) > 3]
            if dirty:
                shown = ', '.join(dirty[:10]) + (f' (+{len(dirty) - 10} lagi)' if len(dirty) > 10 else '')
                lines.append(f'PERINGATAN: {len(dirty)} file berubah lokal (belum di-commit): {shown}')
        if pull_note and 'Already up to date' not in pull_note:
            lines.append('Pull: ' + pull_note.splitlines()[-1][:160])
        test_cmd = cfg.get('testCommand')
        lines.append('Test: ' + (test_cmd if test_cmd else 'tidak terdeteksi') if kind == 'BE' else
                     'Preview: ' + ('tersedia (/preview)' if cfg.get('previewEnabled', True) else 'tidak ada'))
        lines.append('\nMau kerjakan apa?')
        return '\n'.join(lines)

    # ---------- development chat ----------
    def instruction(self, text: str) -> None:
        try:
            name = self._project()
        except DevBotError:
            self.cmd_projects()
            return
        mode = None
        low = text.lower().strip()
        if low.startswith('plan dulu'):
            mode, text = 'plan', text.strip()[len('plan dulu'):].strip(' ,:-') or text
        try:
            sess = self.api.session(name)
            if sess.get('busy'):
                self.say('Masih mengerjakan prompt sebelumnya. Tunggu selesai atau ketik /batal.')
                return
            before = len(sess.get('messages') or [])
            self.api.message(name, text, mode)
        except DevBotError as exc:
            self.say(f'Gagal mengirim: {exc}')
            return
        state = self._state()
        state['last_instruction'] = text
        state['job'] = {'project': name, 'started': self._now(), 'last_progress': self._now(), 'before': before,
                        'last_progress_text': '', 'sent_questions': []}
        self._save(state)
        self.say(f'Diterima, {name} mulai mengerjakan{" (mode plan, tanpa ubah kode)" if mode == "plan" else ""}.')

    def poll_once(self) -> bool:
        """Advance the running job. Returns True while a job is still running."""
        with self._lock:
            return self._poll_once()

    def _poll_once(self) -> bool:
        state = self._state()
        job = state.get('job')
        if not job:
            return False
        name = job['project']
        try:
            sess = self.api.session(name)
        except DevBotError as exc:
            self.say(f'Gagal membaca status {name}: {exc}')
            return True
        for q in sess.get('questions') or []:
            qid = str(q.get('id') or '')
            if qid and qid not in job['sent_questions'] and not q.get('answered'):
                job['sent_questions'].append(qid)
                desc = q.get('title') or q.get('text') or q.get('permission') or json.dumps(q, ensure_ascii=False)[:300]
                self.say(f'{name} minta izin:\n{str(desc)[:600]}',
                         [[{'text': 'Izinkan', 'callback_data': f'dv:perm:{qid}:allowed'},
                           {'text': 'Tolak', 'callback_data': f'dv:perm:{qid}:denied'}]])
        if sess.get('busy'):
            now = self._now()
            if now - job['started'] > self.timeout_s:
                try:
                    self.api.stop(name)
                except DevBotError:
                    pass
                state['job'] = None
                self._save(state)
                self.say(f'Timeout {int(self.timeout_s // 60)} menit, prompt dihentikan.')
                return False
            prog = str(sess.get('progress') or sess.get('live') or '').strip()
            if prog and prog != job.get('last_progress_text') and now - job['last_progress'] >= 45:
                job['last_progress'], job['last_progress_text'] = now, prog
                self.say(f'{name}: {prog[:300]}')
            state['job'] = job
            self._save(state)
            return True
        # finished
        msgs = [m for m in (sess.get('messages') or [])[job.get('before', 0):] if m.get('role') == 'assistant']
        answer = (msgs[-1].get('text') if msgs else '') or '(tidak ada jawaban teks)'
        state['job'] = None
        self._save(state)
        if len(answer) > MAX_TG * 2 and not self.dry_run:
            self.say(answer[:MAX_TG] + '\n... (lengkap dikirim sebagai file)')
            self.telegram.document(self.chat_id, 'jawaban.txt', redact(answer, self._secret_values).encode('utf-8'))
        else:
            self.say(answer)
        if sess.get('last_error'):
            self.say(f"Catatan: {sess['last_error']}")
        self.result_block(name)
        return False

    def run_poll_loop(self) -> None:
        while True:
            try:
                if not self.poll_once():
                    self._sleep(3)
                else:
                    self._sleep(4)
            except Exception as exc:  # never kill the loop
                print(f'[WARN] poll loop: {type(exc).__name__}: {exc}', file=sys.stderr)
                self._sleep(10)

    # ---------- results: FE screenshots / BE tests ----------
    def result_block(self, name: str) -> None:
        try:
            info = self.api.repo(name)
            if self.is_backend(info):
                self.run_tests(name, info)
            else:
                self.screenshots(name)
        except DevBotError as exc:
            self.say(f'Hasil otomatis gagal: {exc}')

    def screenshots(self, name: str) -> None:
        if not self.shot:
            self.say('Screenshot belum aktif (Playwright belum terpasang di server).')
            return
        try:
            pv = self.api.preview_start(name)
            for _ in range(40):
                if pv.get('active') and pv.get('port'):
                    break
                self._sleep(3)
                pv = self.api.preview_status(name)
            if not (pv.get('active') and pv.get('port')):
                self.say('Preview tidak mau jalan. Log terakhir:\n' + '\n'.join((pv.get('log_tail') or [])[-30:]))
                return
            url = f"http://127.0.0.1:{pv['port']}/"
            for label, size in (('desktop', (1280, 800)), ('mobile', (390, 844))):
                data = self.shot(url, size)
                if data and not self.dry_run:
                    self.telegram.photo(self.chat_id, data, f'{name} ({label}) - {time.strftime("%H:%M")}')
        except DevBotError as exc:
            self.say(f'Preview gagal: {exc}')
        except Exception as exc:
            self.say(f'Screenshot gagal: {type(exc).__name__}')

    def run_tests(self, name: str, info: dict[str, Any]) -> None:
        cfg = info.get('config') or {}
        path = info.get('path') or ''
        command = cfg.get('testCommand')
        if not command and (Path(path) / 'vendor' / 'bin' / 'phpunit').exists():
            command = 'vendor/bin/phpunit'
        if not command:
            self.say('Test tidak terdeteksi untuk project ini. Mau saya minta AI menjalankan test? (hasilnya akan ditandai "dilaporkan AI, belum diverifikasi")',
                     [[{'text': 'Ya, minta AI', 'callback_data': 'dv:aitest'}, {'text': 'Tidak', 'callback_data': 'dv:cancel'}]])
            return
        code, out = self.shell(path, command)
        tail = '\n'.join(out.strip().splitlines()[-40:])
        failed = re.findall(r'(?:FAIL|not ok|FAILED)[^\n]{0,100}', out)[:10]
        counts = re.search(r'(\d+) passed|Tests:\s*([^\n]+)|Ran (\d+) tests?', out)
        head = f"TEST {'LULUS' if code == 0 else 'GAGAL'} ({name}): {command}"
        if counts:
            head += f' | {counts.group(0)}'
        if code == 124:
            head = f'TEST TIMEOUT ({name}): {command}'
        body = tail if code != 0 else '\n'.join(tail.splitlines()[-8:])
        extra = ('\nGagal: ' + '; '.join(failed)) if (code != 0 and failed) else ''
        if code != 0 and re.search(r'ECONNREFUSED|could not connect|SQLSTATE|database', out, re.I):
            extra += '\nCatatan: test tampaknya butuh database/env yang tidak ada di VPS, bukan tentu bug kode.'
        self.say(f'{head}{extra}\n{body}')

    def cmd_test(self) -> None:
        name = self._project()
        info = self.api.repo(name)
        if not self.is_backend(info):
            self.say('Project ini FE. Pakai /preview untuk screenshot.')
            return
        self.run_tests(name, info)

    def cmd_preview(self) -> None:
        name = self._project()
        info = self.api.repo(name)
        if self.is_backend(info):
            self.say('Project ini BE, tidak ada preview. Pakai /test.')
            return
        self.screenshots(name)

    # ---------- status / diff / control ----------
    def cmd_status(self) -> None:
        state = self._state()
        name = state.get('project')
        if not name:
            self.say('Belum ada project. /proyek')
            return
        sess = self.api.session(name)
        info = sess.get('repo_info') or self.api.repo(name)
        self.say(f"Project {name} | branch {info.get('current_branch')} | {'SIBUK mengerjakan' if sess.get('busy') else 'siap'}"
                 f"{' | ada perubahan lokal' if info.get('dirty') else ''}\nInstruksi terakhir: {state.get('last_instruction') or '-'}")

    def _files(self, path: str) -> list[str]:
        r = self.git(path, 'status', '--porcelain', '-uall')            # -uall: individual files, never a whole directory
        out = []
        for line in (r.stdout or '').splitlines():
            if len(line) > 3:
                f = line[3:].strip().strip('"')
                if ' -> ' in f:
                    f = f.split(' -> ')[-1]
                out.append(f)
        return out

    def cmd_diff(self) -> None:
        name = self._project()
        info = self.api.repo(name)
        path = info.get('path') or ''
        files = self._files(path)
        stat = (self.git(path, 'diff', '--stat').stdout or '').strip()
        self.say(f'{name}: {len(files)} file berubah\n' + ('\n'.join(files[:30]) or '(bersih)') + (f'\n\n{stat[-1200:]}' if stat else ''))

    def cmd_cancel(self) -> None:
        name = self._project()
        self.api.stop(name)
        state = self._state()
        state['job'] = None
        self._save(state)
        self.say(f'Prompt {name} dihentikan.')

    def cmd_reset(self) -> None:
        name = self._project()
        self.api.reset(name)
        self.say(f'Sesi AI {name} direset.')

    def cmd_close(self) -> None:
        state = self._state()
        name = state.get('project')
        if name:
            try:
                self.api.preview_stop(name)
            except DevBotError:
                pass
        state['project'], state['job'] = None, None
        self._save(state)
        self.say('Project ditutup. /proyek untuk memilih lagi.')

    # ---------- approve = commit + push, only behind a button ----------
    def cmd_approve(self, message: str = '') -> None:
        name = self._project()
        info = self.api.repo(name)
        path = info.get('path') or ''
        files = [f for f in self._files(path) if not SECRET_FILE_RE.search(f) and not JUNK_RE.search(f)]
        skipped = [f for f in self._files(path) if f not in files]
        if not files:
            self.say('Tidak ada perubahan yang bisa di-commit.' + (f' (dilewati: {", ".join(skipped[:5])})' if skipped else ''))
            return
        branch = (self.git(path, 'rev-parse', '--abbrev-ref', 'HEAD').stdout or '').strip()
        instruction = self._state().get('last_instruction') or 'update'
        msg = message or f'feat: {instruction[:60]}'
        target = branch
        if branch in NEVER_PUSH or branch in ('', 'HEAD'):
            target = f'coding/{slug(instruction)}-{time.strftime("%m%d")}'
        card_id = _secrets.token_urlsafe(8)
        state = self._state()
        state.setdefault('cards', {})[card_id] = {'project': name, 'files': files, 'branch': branch, 'target': target,
                                                  'message': msg, 'expires': self._now() + 600, 'used': False}
        self._save(state)
        stat = (self.git(path, 'diff', '--stat').stdout or '').strip().splitlines()
        self.say(f'KONFIRMASI COMMIT DAN PUSH\nProject: {name}\nBranch sekarang: {branch}\nPush ke: {target}'
                 + ('  (branch baru, saya tidak pernah push ke main/master/develop)' if target != branch else '')
                 + f'\nPesan commit: {msg}\nFile ({len(files)}):\n' + '\n'.join('- ' + f for f in files[:30])
                 + (f'\nDilewati otomatis: {", ".join(skipped[:8])}' if skipped else '')
                 + (f'\n\n{stat[-1] if stat else ""}' if stat else ''),
                 [[{'text': 'Ya, commit dan push', 'callback_data': f'dv:ok:{card_id}'},
                   {'text': 'Batal', 'callback_data': 'dv:cancel'}]])

    def do_approve(self, card_id: str) -> None:
        state = self._state()
        card = (state.get('cards') or {}).get(card_id)
        if not card or card.get('used'):
            self.say('Kartu sudah dipakai atau tidak ditemukan.')
            return
        if self._now() > card['expires']:
            self.say('Kartu kedaluwarsa. Ketik /setujui lagi.')
            return
        card['used'] = True                                   # mark first: a double tap can never push twice
        self._save(state)
        name = card['project']
        if not self.allowed(name):
            self.say('Project tidak diizinkan.')
            return
        path = self.api.repo(name).get('path') or ''
        target = card['target']
        if target in NEVER_PUSH:
            self.say('Ditolak: tidak pernah push ke main/master/develop.')
            return
        current = [f for f in self._files(path) if f in card['files']]
        if not current:
            self.say('File yang disetujui sudah tidak berubah. Dibatalkan.')
            return
        steps = []
        if target != card['branch']:
            steps.append(('checkout', ['checkout', '-b', target]))
        steps.append(('add', ['add', '--', *current]))
        steps.append(('commit', ['-c', f'user.name={GIT_NAME}', '-c', f'user.email={GIT_EMAIL}', 'commit', '-m', card['message']]))
        steps.append(('push', ['push', 'origin', target]))
        for label, args in steps:
            r = self.git(path, *args, timeout=300) if label == 'push' else self.git(path, *args)
            if r.returncode != 0:
                self.say(f'{label} gagal, berhenti (tidak ada force): {((r.stderr or r.stdout) or "").strip()[-400:]}')
                return
        head = (self.git(path, 'rev-parse', '--short', 'HEAD').stdout or '').strip()
        self.say(f'Selesai. Commit {head} di-push ke origin/{target}. Saya tidak merge; kamu yang merge.')

    # ---------- callbacks ----------
    def callback(self, data: str) -> None:
        try:
            if data == 'dv:cancel':
                self.say('Dibatalkan.')
            elif data.startswith('dv:pick:'):
                self.select(data[len('dv:pick:'):])
            elif data.startswith('dv:stale:'):
                self.select(data[len('dv:stale:'):], force_stale=True)
            elif data.startswith('dv:perm:'):
                _, _, qid, resp = data.split(':', 3)
                name = self._project()
                self.api.permission(name, qid, 'allowed' if resp == 'allowed' else 'denied')
                self.say('Izin ' + ('diberikan.' if resp == 'allowed' else 'ditolak.'))
            elif data.startswith('dv:ok:'):
                self.do_approve(data[len('dv:ok:'):])
            elif data == 'dv:aitest':
                name = self._project()
                self.api.message(name, 'Run this project\'s tests and report pass/fail counts and failing test names briefly. Do not change code.', 'plan')
                state = self._state()
                state['job'] = {'project': name, 'started': self._now(), 'last_progress': self._now(), 'before': 0,
                                'last_progress_text': '', 'sent_questions': [], 'ai_test': True}
                self._save(state)
                self.say('Meminta AI menjalankan test (hasilnya tidak diverifikasi oleh bot).')
        except DevBotError as exc:
            self.say(f'Gagal: {exc}')


def screenshot_fn() -> Any:
    """Playwright screenshot helper; None when Playwright is not installed."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None

    def shot(url: str, size: tuple[int, int]) -> bytes:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={'width': size[0], 'height': size[1]})
                page.goto(url, wait_until='networkidle', timeout=60000)
                return page.screenshot(full_page=True)
            finally:
                browser.close()
    return shot


def main() -> int:
    ap = argparse.ArgumentParser(description='Telegram dev bot')
    ap.add_argument('--dry-run', action='store_true', help='print messages instead of sending')
    ap.add_argument('--env', default=str(ENV_PATH))
    args = ap.parse_args()
    bot = DevBot(env_path=Path(args.env), dry_run=args.dry_run, shot=screenshot_fn())
    if not bot.chat_id or not bot.env.get('DEV_TELEGRAM_BOT_TOKEN'):
        print('DEV_TELEGRAM_BOT_TOKEN / DEV_TELEGRAM_CHAT_ID belum diisi di .env', file=sys.stderr)
        return 2
    threading.Thread(target=bot.run_poll_loop, daemon=True).start()
    offset = None
    print('[devbot] started', flush=True)
    while True:
        try:
            for update in bot.telegram.updates(offset):
                offset = int(update.get('update_id', 0)) + 1
                try:
                    bot.process_update(update)
                except Exception as exc:
                    print(f'[WARN] update failed: {type(exc).__name__}: {exc}', file=sys.stderr, flush=True)
        except Exception as exc:
            print(f'[WARN] getUpdates: {type(exc).__name__}', file=sys.stderr, flush=True)
            time.sleep(5)


if __name__ == '__main__':
    sys.exit(main())
