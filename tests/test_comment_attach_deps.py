import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

CODE_ROOT = Path(__file__).resolve().parents[1]
ROOT = CODE_ROOT / 'tests/fixtures/project'
PACKAGE = CODE_ROOT / 'src/clickup_gh_workflow'


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ops = load('cad_ops', PACKAGE / 'operations.py')
engine = load('cad_engine', PACKAGE / 'engine.py')
worker = load('cad_worker', PACKAGE / 'worker.py')
runner = load('cad_runner', PACKAGE / 'run.py')
frontend = load('cad_frontend', PACKAGE / 'frontend.py')
CFG = {'list_id': '123'}
REMOTE = {'id': 'abc', 'custom_id': 'DEV-1', 'attachments': [{'title': 'old.png'}]}
PNG = b'\x89PNG\r\n\x1a\nfake'


def api(responses):
    e = Mock()
    e.token.return_value = 'test'
    e.USER_AGENT = engine.USER_AGENT
    e.request.side_effect = lambda method, path, token, body=None: responses(method, path, body)
    return e


def project(tmp):
    root = Path(tmp)
    (root / 'Implementation/mockups').mkdir(parents=True, exist_ok=True)
    (root / '.clickup-workflow.toml').write_text((ROOT / '.clickup-workflow.toml').read_text())
    for spec in (ROOT / 'Implementation').glob('*-spec.md'):
        (root / 'Implementation' / spec.name).write_text(spec.read_text())
    (root / 'Implementation/mockups/screen.png').write_bytes(PNG)
    return root


class CommentTests(unittest.TestCase):
    def setUp(self):
        self.posts = []
        def responses(method, path, body):
            if path.endswith('/member'):
                return {'members': [{'id': 7, 'username': 'lead'}]}
            if method == 'POST':
                self.posts.append(body); return {'id': 'c1'}
            return {'comments': [{'id': 'c1'}]}
        self.e = api(responses)

    def test_preview_never_posts(self):
        result = ops.comment(self.e, CFG, REMOTE, {'text': ' Ready? ', 'mentions': [7]}, False)
        self.assertEqual((result['text'], result['mentions']), ('Ready?', ['lead']))
        self.assertEqual(self.posts, [])

    def test_apply_tags_members_and_verifies(self):
        result = ops.comment(self.e, CFG, REMOTE, {'text': 'Ready?', 'mentions': [7]}, True)
        self.assertTrue(result['verified'])
        self.assertEqual(self.posts[0]['comment'], [{'type': 'tag', 'user': {'id': 7}}, {'text': ' Ready?'}])
        self.assertFalse(self.posts[0]['notify_all'])

    def test_rejects_empty_text_and_unknown_mentions(self):
        with self.assertRaisesRegex(ValueError, 'empty'):
            ops.comment(self.e, CFG, REMOTE, {'text': '  '}, True)
        with self.assertRaisesRegex(ValueError, 'List members'):
            ops.comment(self.e, CFG, REMOTE, {'text': 'hi', 'mentions': [8]}, True)
        self.assertEqual(self.posts, [])


class AttachTests(unittest.TestCase):
    def test_preview_and_duplicate_never_upload(self):
        with tempfile.TemporaryDirectory() as tmp, patch('urllib.request.urlopen') as urlopen:
            root = project(tmp)
            e = api(lambda *a: self.fail('no request expected'))
            result = ops.attach(e, root, REMOTE, {'file': 'Implementation/mockups/screen.png'}, False)
            self.assertEqual((result['bytes'], result['already_attached']), (len(PNG), False))
            (root / 'Implementation/mockups/old.png').write_bytes(PNG)
            self.assertTrue(ops.attach(e, root, REMOTE, {'file': 'Implementation/mockups/old.png'}, True)['already_attached'])
            urlopen.assert_not_called()

    def cli(self, root, *words):
        with patch('builtins.print'), patch.object(frontend, 'module') as module:
            local = Mock(); local.execute.return_value = {}
            module.side_effect = [Mock(), Mock(), local]
            frontend.main(['--root', str(root), '--via', 'local', *words],
                          {'tomllib': __import__('tomllib'), 'engine': Mock(return_value=Mock())})
            return local.execute.call_args.args

    def test_cli_only_sends_existing_png_under_mockups(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = project(tmp)
            (root / 'secret.png').write_bytes(PNG)
            (root / 'Implementation/mockups/notes.txt').write_text('x')
            (root / 'Implementation/mockups/link.png').symlink_to(root / 'secret.png')
            for path in ('secret.png', 'Implementation/mockups/notes.txt', 'Implementation/mockups/missing.png',
                         'Implementation/mockups/../../secret.png', 'Implementation/mockups/link.png'):
                with self.assertRaisesRegex(ValueError, 'Attach only'):
                    self.cli(root, 'attach', 'DEV-1', '--file', path)
            args = self.cli(root, 'attach', 'DEV-1', '--file', 'Implementation/mockups/screen.png')
            self.assertEqual(args[3:], ('attach', 'DEV-1', False, {'file': 'Implementation/mockups/screen.png'}))

    def test_cli_comment_reads_text_file_and_mentions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = project(tmp)
            (root / 'note.md').write_text('Question for the lead\n')
            args = self.cli(root, 'comment', 'DEV-1', '--text-file', str(root / 'note.md'), '--mention', '7', '--apply')
            self.assertEqual(args[3:], ('comment', 'DEV-1', True, {'text': 'Question for the lead', 'mentions': [7]}))

    def test_github_payload_carries_only_the_attached_mockup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = project(tmp)
            (root / 'Implementation/mockups/other.png').write_bytes(PNG)
            with patch.object(runner, 'command') as command, patch.object(runner, 'gh_json') as gh:
                _, receipt = runner.prepare(root, 'attach', 'DEV-1', True, True, options={'file': 'Implementation/mockups/screen.png'})
            command.assert_not_called(); gh.assert_not_called()
            mockups = Path(receipt['staging']) / '.team-workflow-runs' / receipt['request_id'] / 'project/Implementation/mockups'
            self.assertEqual([p.name for p in mockups.iterdir()], ['screen.png'])


class LinkDepsTests(unittest.TestCase):
    def run_link(self, root, existing, apply):
        posts = []
        e = api(lambda method, path, body: posts.append((path, body)) or {})
        e.parse_cards, e.TASK_ID = engine.parse_cards, engine.TASK_ID
        deps = [{'task_id': 't20', 'depends_on': 't10'}] if existing else []
        e.all_tasks.side_effect = [[{'id': 't10'}, {'id': 't20', 'dependencies': deps}],
                                   [{'id': 't10'}, {'id': 't20', 'dependencies': [{'task_id': 't20', 'depends_on': 't10'}]}]]
        return ops.link_deps(e, CFG, root, apply), posts

    def bound(self, tmp, specs=('APP-010', 'APP-020')):
        root = project(tmp)
        ids = {'APP-010': 't10', 'APP-020': 't20'}
        (root / 'Implementation/clickup-bindings.json').write_text(json.dumps({s: {'id': ids[s], 'custom_id': 'DEV-' + ids[s][1:]} for s in specs}))
        return root

    def test_preview_lists_missing_links_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, posts = self.run_link(self.bound(tmp), False, False)
            self.assertEqual(posts, [])
            self.assertEqual(result['to_create'], 1)
            row = [c for c in result['cards'] if c['spec_id'] == 'APP-020'][0]
            self.assertEqual(row['waits_on'], [{'spec_id': 'APP-010', 'custom_id': 'DEV-10', 'action': 'create'}])

    def test_apply_creates_then_verifies_and_skips_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, posts = self.run_link(self.bound(tmp), False, True)
            self.assertEqual(posts, [('/task/t20/dependency', {'depends_on': 't10'})])
            self.assertTrue(result['verified'])
            result, posts = self.run_link(self.bound(tmp), True, True)
            self.assertEqual((posts, result['to_create']), ([], 0))

    def test_unbound_dependency_is_reported_not_linked(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, posts = self.run_link(self.bound(tmp, ('APP-020',)), False, True)
            self.assertEqual(posts, [])
            self.assertEqual(result['cards'][0]['unbound'], ['APP-010'])


class SprintsTests(unittest.TestCase):
    def test_reads_only_sprint_folders(self):
        e = Mock()
        e.request.side_effect = lambda method, path, token: (
            {'name': 'Board', 'space': {'id': 's1'}} if path == '/list/123' else
            {'folders': [{'id': 'f1', 'name': 'Sprint Folder', 'lists': [{'id': 'l1', 'name': 'Sprint 1', 'task_count': 3, 'extra': 'x'}]},
                         {'id': 'f2', 'name': 'Archive', 'lists': []}]})
        result = worker.execute(e, CFG, ROOT, 'sprints', None)
        self.assertEqual(result['sprint_folders'][0]['lists'][0],
                         {'id': 'l1', 'name': 'Sprint 1', 'start_date': None, 'due_date': None, 'task_count': 3})
        self.assertEqual((result['all_folder_names'], result['errors']), (['Sprint Folder', 'Archive'], []))
        with self.assertRaisesRegex(ValueError, 'Read-only'):
            worker.execute(e, CFG, ROOT, 'sprints', None, True)

    def test_github_validation_accepts_new_operations(self):
        cfg = {'repository': 'a/b', 'fork': 'c/b', 'base_branch': 'main'}
        runner.validate(cfg, 'sprints', None, False)
        runner.validate(cfg, 'link-deps', None, True)
        runner.validate(cfg, 'comment', 'DEV-1', True)
        with self.assertRaises(ValueError):
            runner.validate(cfg, 'sprints', None, True)


if __name__ == '__main__':
    unittest.main()
