import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from devbot.telegram_dev_bot import DevBot, DevBotError, redact

TOKEN = 'TOKEN123456'
REPOS = [
    {'name': 'sea-map-cms', 'path': '/p/sea-map-cms', 'dirty': False, 'current_branch': 'staging',
     'origin_url': 'ssh://git@forge.example/x/sea-map-cms.git', 'config': {'type': 'frontend', 'previewEnabled': True}},
    {'name': 'sea-map-api', 'path': '/p/sea-map-api', 'dirty': True, 'current_branch': 'main',
     'origin_url': 'https://user:SECRETPASS@forge.example/x/sea-map-api.git',
     'config': {'type': 'api', 'previewEnabled': False, 'testCommand': 'npm test'}},
    {'name': 'trading-brain', 'path': '/p/trading-brain', 'dirty': False, 'current_branch': 'master', 'config': {}},
    {'name': 'second-brain', 'path': '/p/second-brain', 'dirty': False, 'current_branch': 'main', 'config': {}},
]


class FakeApi:
    def __init__(self):
        self.calls = []
        self.busy = False
        self.questions = []
        self.messages = []
        self.pull_result = {'ok': True, 'output': 'Already up to date.'}
        self.preview = {'active': True, 'port': 4101}

    def repos(self):
        return list(REPOS)

    def repo(self, name):
        for r in REPOS:
            if r['name'] == name:
                return dict(r)
        raise DevBotError('repo not found')

    def pull(self, name):
        self.calls.append(('pull', name))
        res = dict(self.pull_result)
        res['repo'] = self.repo(name)
        return res

    def session(self, name):
        return {'busy': self.busy, 'messages': list(self.messages), 'questions': list(self.questions),
                'progress': 'editing files', 'repo_info': self.repo(name)}

    def message(self, name, text, mode=None):
        self.calls.append(('message', name, text, mode))
        return {}

    def permission(self, name, qid, response):
        self.calls.append(('permission', name, qid, response))

    def stop(self, name):
        self.calls.append(('stop', name))

    def reset(self, name):
        self.calls.append(('reset', name))

    def preview_start(self, name):
        self.calls.append(('preview_start', name))
        return dict(self.preview)

    def preview_status(self, name):
        return dict(self.preview)

    def preview_stop(self, name):
        self.calls.append(('preview_stop', name))


class FakeTelegram:
    def __init__(self):
        self.sent, self.photos, self.docs = [], [], []

    def send(self, chat_id, text, keyboard=None):
        self.sent.append((text, keyboard))

    def photo(self, chat_id, data, caption=''):
        self.photos.append((data, caption))

    def document(self, chat_id, name, data, caption=''):
        self.docs.append(name)

    def answer(self, callback_id):
        pass


class FakeGit:
    def __init__(self):
        self.calls = []
        self.status = ' M src/app.js\n?? .env\n?? node_modules/x/index.js\n?? notes.md\n'
        self.branch = 'staging'
        self.fail = None

    def __call__(self, path, *args, timeout=60):
        self.calls.append(args)
        out = ''
        if args[:1] == ('status',):
            out = self.status
        elif args[:2] == ('rev-parse', '--abbrev-ref'):
            out = self.branch + '\n'
        elif args[:2] == ('rev-parse', '--short'):
            out = 'abc1234\n'
        elif args[:1] == ('log',):
            out = 'a1b2c3 fix login (Rendy, 2 days ago)\nd4e5f6 add page (Anggun, 3 days ago)\n'
        elif args[:1] == ('rev-list',):
            out = '0\t2\n'
        elif args[:2] == ('diff', '--stat'):
            out = ' src/app.js | 2 +-\n 1 file changed\n'
        code = 1 if (self.fail and self.fail in args) else 0
        return subprocess.CompletedProcess(args, code, out, 'rejected' if code else '')


class DevBotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.clock = [1000.0]
        self.build()

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, extra_env='', shot=None, shell=None):
        env = self.root / '.env'
        env.write_text(f'DEV_TELEGRAM_BOT_TOKEN={TOKEN}\nDEV_TELEGRAM_CHAT_ID=42\nOTHER_SECRET=ZZZZZZZZ\n{extra_env}')
        self.api, self.tg, self.git = FakeApi(), FakeTelegram(), FakeGit()
        self.bot = DevBot(env_path=env, state_path=self.root / 'state.json', api=self.api, telegram=self.tg, git=self.git,
                          shot=shot, shell=shell or (lambda path, cmd: (0, 'ok\n5 passed\n')),
                          now_fn=lambda: self.clock[0], sleep_fn=lambda s: None)

    def msg(self, text, chat=42):
        self.bot.process_update({'message': {'chat': {'id': chat}, 'text': text}})

    def tap(self, data, chat=42):
        self.bot.process_update({'callback_query': {'id': 'c', 'data': data, 'message': {'chat': {'id': chat}}}})

    def last(self):
        return self.tg.sent[-1][0]

    def texts(self):
        return '\n'.join(t for t, _ in self.tg.sent)

    # --- access control ---
    def test_foreign_chat_is_ignored(self):
        self.msg('/proyek', chat=7)
        self.tap('dv:pick:sea-map-cms', chat=7)
        self.assertEqual([], self.tg.sent)
        self.assertEqual([], self.api.calls)

    def test_denied_repos_are_never_listed_and_cannot_be_selected(self):
        self.msg('/proyek')
        keyboard = self.tg.sent[-1][1]
        labels = ' '.join(b['text'] for row in keyboard for b in row)
        self.assertIn('sea-map-cms', labels)
        self.assertNotIn('trading-brain', labels)
        self.assertNotIn('second-brain', labels)
        for name in ('trading-brain', 'second-brain'):
            self.tap(f'dv:pick:{name}')
            self.msg(f'/mulai {name}')
        self.assertEqual([], [c for c in self.api.calls if c[0] == 'pull'])
        self.assertIsNone(self.bot._state().get('project'))

    def test_hard_deny_cannot_be_removed_by_env_or_allowlist(self):
        self.build('DEVBOT_REPO_ALLOWLIST=trading-brain,second-brain,sea-map-cms\nDEVBOT_REPO_DENYLIST=\n')
        self.assertFalse(self.bot.allowed('trading-brain'))
        self.assertFalse(self.bot.allowed('second-brain'))
        self.assertTrue(self.bot.allowed('sea-map-cms'))

    def test_allowlist_restricts_the_list(self):
        self.build('DEVBOT_REPO_ALLOWLIST=sea-map-api\n')
        self.assertEqual(['sea-map-api'], [r['name'] for r in self.bot.list_repos()])

    # --- select = pull + briefing ---
    def test_selecting_pulls_first_then_sends_the_briefing(self):
        self.tap('dv:pick:sea-map-cms')
        self.assertEqual(('pull', 'sea-map-cms'), self.api.calls[0])
        text = self.last()
        self.assertIn('PROJECT sea-map-cms', text)
        self.assertIn('fix login', text)
        self.assertIn('Mau kerjakan apa?', text)
        self.assertEqual('sea-map-cms', self.bot._state()['project'])

    def test_failed_pull_does_not_continue_silently(self):
        self.api.pull_result = {'ok': False, 'output': 'fatal: Not possible to fast-forward'}
        self.tap('dv:pick:sea-map-cms')
        self.assertIn('GAGAL', self.last())
        self.assertIn('tidak lanjut diam-diam', self.last())
        self.assertIsNone(self.bot._state().get('project'))
        buttons = [b['callback_data'] for row in self.tg.sent[-1][1] for b in row]
        self.assertIn('dv:stale:sea-map-cms', buttons)
        self.tap('dv:stale:sea-map-cms')                                   # the owner chooses to continue anyway
        self.assertEqual('sea-map-cms', self.bot._state()['project'])
        self.assertIn('TANPA PULL', self.last())

    def test_briefing_strips_credentials_from_the_remote(self):
        self.tap('dv:pick:sea-map-api')
        self.assertNotIn('SECRETPASS', self.texts())
        self.assertIn('***@', self.texts())
        self.assertIn('Test: npm test', self.last())
        self.assertIn('PERINGATAN', self.last())                            # the dirty files warning

    def test_plain_text_without_a_project_shows_the_list(self):
        self.msg('perbaiki bug login')
        self.assertIn('Pilih project', self.last())
        self.assertEqual([], [c for c in self.api.calls if c[0] == 'message'])

    # --- development chat ---
    def test_instruction_is_sent_and_a_second_one_is_refused_while_busy(self):
        self.tap('dv:pick:sea-map-cms')
        self.msg('perbaiki tombol login')
        self.assertIn(('message', 'sea-map-cms', 'perbaiki tombol login', None), self.api.calls)
        self.api.busy = True
        self.msg('tambah lagi')
        self.assertIn('Masih mengerjakan', self.last())
        self.assertEqual(1, len([c for c in self.api.calls if c[0] == 'message']))

    def test_plan_prefix_uses_plan_mode(self):
        self.tap('dv:pick:sea-map-cms')
        self.msg('plan dulu: ubah halaman login')
        self.assertIn(('message', 'sea-map-cms', 'ubah halaman login', 'plan'), self.api.calls)

    def test_progress_is_bounded_and_final_answer_is_sent_with_a_result(self):
        self.build(shot=lambda url, size: b'PNG')
        self.tap('dv:pick:sea-map-cms')
        self.msg('perbaiki tombol')
        self.api.busy = True
        self.clock[0] += 10
        self.assertTrue(self.bot.poll_once())                               # under 45 s: no progress message yet
        count = len(self.tg.sent)
        self.clock[0] += 60
        self.assertTrue(self.bot.poll_once())
        self.assertEqual(count + 1, len(self.tg.sent))                      # one progress message
        self.assertTrue(self.bot.poll_once())
        self.assertEqual(count + 1, len(self.tg.sent))                      # same text again: not repeated
        self.api.busy = False
        self.api.messages = [{'role': 'user', 'text': 'x'}, {'role': 'assistant', 'text': 'Sudah saya perbaiki.'}]
        self.assertFalse(self.bot.poll_once())
        self.assertIn('Sudah saya perbaiki.', self.texts())
        self.assertEqual(2, len(self.tg.photos))                            # desktop + mobile screenshots
        self.assertIn(('preview_start', 'sea-map-cms'), self.api.calls)

    def test_permission_question_is_never_auto_approved(self):
        self.tap('dv:pick:sea-map-cms')
        self.msg('hapus file lama')
        self.api.busy = True
        self.api.questions = [{'id': 'q1', 'title': 'Run rm -rf build?'}]
        self.bot.poll_once()
        self.assertIn('minta izin', self.last())
        self.assertEqual([], [c for c in self.api.calls if c[0] == 'permission'])
        buttons = [b['callback_data'] for row in self.tg.sent[-1][1] for b in row]
        self.assertEqual(['dv:perm:q1:allowed', 'dv:perm:q1:denied'], buttons)
        self.bot.poll_once()                                                 # not asked twice
        self.assertEqual(1, self.texts().count('minta izin'))
        self.tap('dv:perm:q1:denied')
        self.assertIn(('permission', 'sea-map-cms', 'q1', 'denied'), self.api.calls)

    def test_timeout_stops_the_prompt(self):
        self.build('DEVBOT_JOB_TIMEOUT_MIN=1\n')
        self.tap('dv:pick:sea-map-cms')
        self.msg('kerjakan')
        self.api.busy = True
        self.clock[0] += 120
        self.assertFalse(self.bot.poll_once())
        self.assertIn(('stop', 'sea-map-cms'), self.api.calls)
        self.assertIn('Timeout', self.last())

    # --- FE / BE results ---
    def test_fe_without_playwright_says_so(self):
        self.tap('dv:pick:sea-map-cms')
        self.msg('x')
        self.api.messages = [{'role': 'assistant', 'text': 'ok'}]
        self.bot.poll_once()
        self.assertIn('Screenshot belum aktif', self.texts())

    def test_fe_preview_failure_is_reported(self):
        self.build(shot=lambda url, size: b'PNG')
        self.api.preview = {'active': False, 'port': None, 'log_tail': ['npm ERR! missing script: dev']}
        self.tap('dv:pick:sea-map-cms')
        self.bot.cmd_preview()
        self.assertIn('Preview tidak mau jalan', self.last())
        self.assertIn('missing script', self.last())

    def test_be_runs_the_detected_test_command_and_reports(self):
        ran = []
        self.build(shell=lambda path, cmd: (ran.append((path, cmd)) or (1, 'FAIL login test\nSQLSTATE connection refused\n3 passed')))
        self.tap('dv:pick:sea-map-api')
        self.msg('/test')
        self.assertEqual([('/p/sea-map-api', 'npm test')], ran)
        text = self.last()
        self.assertIn('TEST GAGAL', text)
        self.assertIn('butuh database', text)

    def test_be_without_a_test_command_offers_the_ai_fallback(self):
        REPOS[1]['config'].pop('testCommand')
        try:
            self.tap('dv:pick:sea-map-api')
            self.msg('/test')
            self.assertIn('Test tidak terdeteksi', self.last())
            self.assertIn('dv:aitest', str(self.tg.sent[-1][1]))
        finally:
            REPOS[1]['config']['testCommand'] = 'npm test'

    def test_fe_project_refuses_test_and_be_refuses_preview(self):
        self.tap('dv:pick:sea-map-cms')
        self.msg('/test')
        self.assertIn('FE', self.last())
        self.tap('dv:pick:sea-map-api')
        self.msg('/preview')
        self.assertIn('BE', self.last())

    # --- approve = commit + push ---
    def test_approve_card_lists_only_safe_files_and_asks_for_a_tap(self):
        self.tap('dv:pick:sea-map-cms')
        self.msg('/setujui fix: login button')
        text = self.last()
        self.assertIn('src/app.js', text)
        self.assertIn('notes.md', text)
        self.assertNotIn('- .env', text)
        self.assertNotIn('- node_modules', text)
        self.assertIn('Dilewati otomatis', text)
        self.assertIn('Pesan commit: fix: login button', text)
        self.assertEqual([], [a for a in self.git.calls if a[:1] in (('commit',), ('push',), ('add',))])
        self.assertIn(('status', '--porcelain', '-uall'), self.git.calls)

    def test_tap_commits_and_pushes_only_the_approved_files_without_force(self):
        self.tap('dv:pick:sea-map-cms')
        self.msg('/setujui')
        card_id = next(iter(self.bot._state()['cards']))
        self.tap(f'dv:ok:{card_id}')
        adds = [a for a in self.git.calls if a[:1] == ('add',)]
        self.assertEqual([('add', '--', 'src/app.js', 'notes.md')], adds)
        pushes = [a for a in self.git.calls if a[:1] == ('push',)]
        self.assertEqual([('push', 'origin', 'staging')], pushes)
        flat = ' '.join(' '.join(a) for a in self.git.calls)
        self.assertNotIn('--force', flat)
        self.assertNotIn('-f ', flat)
        self.assertIn('Saya tidak merge', self.last())

    def test_main_branch_gets_a_feature_branch_never_a_push_to_main(self):
        self.git.branch = 'main'
        self.tap('dv:pick:sea-map-cms')
        self.msg('perbaiki login')
        self.msg('/setujui')
        self.assertIn('branch baru', self.last())
        card = next(iter(self.bot._state()['cards'].values()))
        self.assertTrue(card['target'].startswith('coding/'))
        self.tap(f"dv:ok:{next(iter(self.bot._state()['cards']))}")
        pushes = [a for a in self.git.calls if a[:1] == ('push',)]
        self.assertEqual(1, len(pushes))
        self.assertTrue(pushes[0][2].startswith('coding/'))
        self.assertIn(('checkout', '-b', pushes[0][2]), self.git.calls)

    def test_double_tap_pushes_once_and_expired_cards_are_refused(self):
        self.tap('dv:pick:sea-map-cms')
        self.msg('/setujui')
        card_id = next(iter(self.bot._state()['cards']))
        self.tap(f'dv:ok:{card_id}')
        self.tap(f'dv:ok:{card_id}')
        self.assertEqual(1, len([a for a in self.git.calls if a[:1] == ('push',)]))
        self.assertIn('sudah dipakai', self.last())
        self.msg('/setujui')
        card2 = [k for k in self.bot._state()['cards'] if k != card_id][0]
        self.clock[0] += 1000
        self.tap(f'dv:ok:{card2}')
        self.assertIn('kedaluwarsa', self.last())
        self.assertEqual(1, len([a for a in self.git.calls if a[:1] == ('push',)]))

    def test_push_failure_is_reported_and_stops(self):
        self.git.fail = 'push'
        self.tap('dv:pick:sea-map-cms')
        self.msg('/setujui')
        self.tap(f"dv:ok:{next(iter(self.bot._state()['cards']))}")
        self.assertIn('push gagal', self.last())
        self.assertIn('tidak ada force', self.last())

    def test_nothing_to_commit(self):
        self.git.status = '?? .env\n'
        self.tap('dv:pick:sea-map-cms')
        self.msg('/setujui')
        self.assertIn('Tidak ada perubahan yang bisa di-commit', self.last())

    # --- secrets ---
    def test_token_never_appears_in_outgoing_text(self):
        self.bot.say(f'oops {TOKEN} https://u:p4ss@host/x.git')
        self.assertNotIn(TOKEN, self.texts())
        self.assertNotIn('p4ss', self.texts())

    def test_only_the_bots_own_keys_are_read_from_env(self):
        self.assertNotIn('OTHER_SECRET', self.bot.env)
        self.assertEqual(TOKEN, self.bot.env['DEV_TELEGRAM_BOT_TOKEN'])

    def test_redact_helper(self):
        self.assertEqual('a [REDACTED] b', redact('a SECRETVAL b', ['SECRETVAL']))
        self.assertEqual('https://***@h/x', redact('https://tok3n@h/x', []))


if __name__ == '__main__':
    unittest.main()
