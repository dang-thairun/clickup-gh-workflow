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


ops = load('listing_ops', PACKAGE / 'operations.py')
worker = load('listing_worker', PACKAGE / 'worker.py')
runner = load('listing_runner', PACKAGE / 'run.py')
frontend = load('listing_frontend', PACKAGE / 'frontend.py')

MEMBERS = [
    {'id': 77, 'username': 'Dang Thairun', 'email': 'dang@example.invalid'},
    {'id': 88, 'username': 'Somchai', 'email': 'somchai@example.invalid'},
    {'id': 99, 'username': 'somchai', 'email': 'somchai2@example.invalid'},
]


def task(ident, assignees, status='in progress', kind='custom', **extra):
    value = {'id': ident, 'custom_id': 'TASK-' + ident, 'name': extra.pop('name', 'Task ' + ident),
             'list': {'id': '123'}, 'status': {'status': status, 'type': kind},
             'assignees': [{'id': a, 'username': 'u' + str(a)} for a in assignees]}
    value.update(extra)
    return value


class TaskListingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cfg = {'list_id': '123'}
        self.current = {'id': 77, 'username': 'Dang Thairun'}
        self.engine = Mock()

        def request(method, path, token, body=None):
            if path.endswith('/member'):
                return {'members': MEMBERS}
            if path == '/user':
                return {'user': self.current}
            raise AssertionError('Unexpected request ' + path)
        self.engine.request.side_effect = request
        self.engine.all_tasks.return_value = [
            task('1', [77], name='[APP-010] Mine', due_date='2000'),
            task('2', [88], due_date='1000'),
            task('3', [77], status='complete', kind='closed'),
            task('4', [77], archived=True),
            dict(task('5', [77]), list={'id': '999'}),
            task('6', [77, 88]),
        ]

    def ids(self, result):
        return [t['id'] for t in result['tasks']]

    def test_unfiltered_lists_open_tasks_sorted_by_due_and_skips_foreign_closed_archived(self):
        result = ops.list_tasks(self.engine, self.cfg, self.root, {})
        self.assertEqual(self.ids(result), ['2', '1', '6'])
        self.assertIsNone(result['assignee'])
        self.engine.request.assert_not_called()

    def test_include_closed_adds_closed_tasks(self):
        result = ops.list_tasks(self.engine, self.cfg, self.root, {'include_closed': True})
        self.assertIn('3', self.ids(result))

    def test_mine_resolves_token_owner_and_warns_about_shared_tokens(self):
        result = ops.list_tasks(self.engine, self.cfg, self.root, {'mine': True})
        self.assertEqual(self.ids(result), ['1', '6'])
        self.assertEqual(result['assignee']['id'], 77)
        self.assertEqual(result['assignee_source'], 'token-owner')
        self.assertIn('shared', result['note'])

    def test_mine_stops_and_asks_when_token_owner_is_not_a_list_member(self):
        self.current = {'id': 5, 'username': 'team-bot'}
        with self.assertRaisesRegex(ValueError, 'team-bot.*--assignee'):
            ops.list_tasks(self.engine, self.cfg, self.root, {'mine': True})
        self.engine.all_tasks.assert_not_called()

    def test_assignee_matches_id_email_or_username_case_insensitively(self):
        for selector in ('77', 'DANG@example.invalid', 'dang thairun'):
            result = ops.list_tasks(self.engine, self.cfg, self.root, {'assignee': selector})
            self.assertEqual(result['assignee']['id'], 77, selector)
            self.assertEqual(result['assignee_source'], 'argument')

    def test_ambiguous_username_requires_numeric_id(self):
        with self.assertRaisesRegex(ValueError, 'several.*88.*99'):
            ops.list_tasks(self.engine, self.cfg, self.root, {'assignee': 'SOMCHAI'})

    def test_unknown_assignee_suggests_close_names_and_never_guesses(self):
        with self.assertRaisesRegex(ValueError, 'No member.*Did you mean: Dang Thairun'):
            ops.list_tasks(self.engine, self.cfg, self.root, {'assignee': 'Dang Thairn'})
        self.engine.all_tasks.assert_not_called()

    def test_bound_spec_ids_come_from_bindings(self):
        (self.root / 'Implementation').mkdir()
        (self.root / 'Implementation/clickup-bindings.json').write_text(json.dumps({'APP-020': {'id': '6'}}))
        result = ops.list_tasks(self.engine, self.cfg, self.root, {'mine': True})
        self.assertEqual({t['id']: t['spec_id'] for t in result['tasks']}, {'1': 'APP-010', '6': 'APP-020'})

    def test_listing_never_writes(self):
        ops.list_tasks(self.engine, self.cfg, self.root, {'mine': True})
        self.assertTrue(all(c.args[0] == 'GET' for c in self.engine.request.call_args_list))


class TaskCommandWiringTests(unittest.TestCase):
    def test_worker_routes_tasks_and_rejects_apply(self):
        engine = Mock()
        engine.all_tasks.return_value = []
        self.assertEqual(worker.execute(engine, {'list_id': '123'}, ROOT, 'tasks', None, False, {})['total'], 0)
        with self.assertRaisesRegex(ValueError, 'Read-only'):
            worker.execute(engine, {'list_id': '123'}, ROOT, 'tasks', None, True, {})

    def test_github_request_accepts_tasks_without_an_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.clickup-workflow.toml').write_text((ROOT / '.clickup-workflow.toml').read_text())
            with patch.object(runner, 'command') as command, patch.object(runner, 'gh_json') as gh:
                _, receipt = runner.prepare(root, 'tasks', None, False, True, options={'mine': True})
            command.assert_not_called(); gh.assert_not_called()
            request = json.loads((Path(receipt['staging']) / '.team-workflow-runs' / receipt['request_id'] / 'request.json').read_text())
            self.assertEqual((request['operation'], request['options']), ('tasks', {'mine': True}))
        with self.assertRaises(ValueError):
            runner.validate({'repository': 'a/b', 'fork': 'c/b', 'base_branch': 'main'}, 'tasks', None, True)

    def run_cli(self, root, *words, value=None):
        with patch('builtins.print'), patch.object(frontend, 'module') as module:
            local = Mock()
            local.execute.return_value = value or {'assignee': None, 'tasks': []}
            module.side_effect = [Mock(), Mock(), local]
            frontend.main(['--root', str(root), '--via', 'local', 'tasks', *words],
                          {'tomllib': __import__('tomllib'), 'engine': Mock(return_value=Mock())})
            return local.execute.call_args.args[-1]

    def test_cli_uses_saved_identity_and_remember_writes_it_locally(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.clickup-workflow.toml').write_text('list_id="123"')
            self.assertEqual(self.run_cli(root, '--mine'), {'include_closed': False, 'mine': True})
            person = {'id': 88, 'username': 'Somchai', 'email': 'somchai@example.invalid'}
            options = self.run_cli(root, '--assignee', 'somchai@example.invalid', '--remember', value={'assignee': person})
            self.assertEqual(options['assignee'], 'somchai@example.invalid')
            self.assertEqual(json.loads((root / '.team-workflow/me.json').read_text()), person)
            self.assertEqual(self.run_cli(root, '--mine'), {'include_closed': False, 'assignee': '88', 'assignee_source': 'saved'})

    def test_remember_requires_a_selector(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / '.clickup-workflow.toml').write_text('list_id="123"')
            with self.assertRaisesRegex(ValueError, '--remember'):
                self.run_cli(root, '--remember')


if __name__ == '__main__':
    unittest.main()
