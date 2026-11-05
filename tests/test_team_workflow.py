import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

CODE_ROOT = Path(__file__).resolve().parents[1]
ROOT = CODE_ROOT / 'tests/fixtures/project'
PACKAGE = CODE_ROOT / 'src/clickup_gh_workflow'
def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result
cli = module('team', PACKAGE / 'engine.py')
CFG = cli.tomllib.loads((ROOT / '.clickup-workflow.toml').read_text())

class WorkflowTests(unittest.TestCase):
    def pr(self, **changes):
        result = {'number': 9, 'base': {'repo': {'full_name': CFG['repository']}, 'ref': 'develop'},
                  'head': {'ref': 'feat/DEV-42-example'}, 'body': 'ClickUp-Task: DEV-42\n',
                  'author_association': 'MEMBER', 'state': 'open', 'draft': False}
        result.update(changes)
        return result

    def test_parse_real_plan(self):
        cards = cli.parse_cards(ROOT)
        self.assertEqual(len(cards), 2)
        self.assertEqual(len(cards), len({c['spec_id'] for c in cards}))
        self.assertTrue(all(c['subtasks'] for c in cards))

    def test_draft_and_review(self):
        self.assertEqual(cli.desired_status(CFG, self.pr(draft=True), []), 'in progress')
        self.assertEqual(cli.desired_status(CFG, self.pr(), []), 'in review')

    def test_latest_review_per_reviewer(self):
        reviews = [{'id': 1, 'user': {'login': 'lead'}, 'state': 'CHANGES_REQUESTED'},
                   {'id': 2, 'user': {'login': 'lead'}, 'state': 'APPROVED'}]
        self.assertEqual(cli.desired_status(CFG, self.pr(), reviews[:1]), 'rework')
        self.assertEqual(cli.desired_status(CFG, self.pr(), reviews), 'in review')

    def test_other_reviewer_unresolved(self):
        reviews = [{'id': 1, 'user': {'login': 'a'}, 'state': 'CHANGES_REQUESTED'},
                   {'id': 2, 'user': {'login': 'b'}, 'state': 'APPROVED'}]
        self.assertEqual(cli.desired_status(CFG, self.pr(), reviews), 'rework')

    def test_merge_is_not_qa_ready(self):
        self.assertIsNone(cli.desired_status(CFG, self.pr(merged=True), []))

    def test_untrusted_author_rejected(self):
        with self.assertRaises(ValueError):
            cli.desired_status(CFG, self.pr(author_association='NONE'), [])

    def test_wrong_base_rejected(self):
        pr = self.pr(); pr['base']['ref'] = 'main'
        with self.assertRaises(ValueError):
            cli.desired_status(CFG, pr, [])

    def sync_response(self, pr=None, status='done'):
        def response(method, path, *args, **kwargs):
            if '/reviews?' in path: return []
            if '/pulls/' in path: return pr or self.pr()
            if '/task/' in path: return {'id': 'internal', 'list': {'id': CFG['list_id']}, 'status': {'status': status}}
            raise AssertionError((method, path))
        return response

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test', 'GITHUB_TOKEN': 'test'})
    def test_terminal_state_never_changes(self):
        with patch.object(cli, 'request', side_effect=self.sync_response()) as mock:
            result = cli.sync_pr(CFG, {'pull_request': self.pr()}, True)
            self.assertEqual(result['reason'], 'protected state')
            self.assertFalse(any(c.args[0] == 'PUT' for c in mock.call_args_list))

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test', 'GITHUB_TOKEN': 'test'})
    def test_branch_body_mismatch(self):
        pr = self.pr(); pr['head']['ref'] = 'feat/DEV-421-wrong'
        with patch.object(cli, 'request', side_effect=self.sync_response(pr)):
            with self.assertRaisesRegex(ValueError, 'Branch'):
                cli.sync_pr(CFG, {'pull_request': pr}, True)

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test'})
    def test_ready_gate(self):
        with patch.object(cli, 'get_task', return_value={'status': {'status': 'backlog'}}):
            with self.assertRaisesRegex(ValueError, 'not READY'):
                cli.start_task(CFG, 'DEV-42', ROOT)

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test'})
    def test_preview_does_not_write(self):
        card = cli.parse_cards(ROOT)[0]
        with tempfile.TemporaryDirectory() as tmp, patch.object(cli, 'doctor'), patch.object(cli, 'all_tasks', return_value=[]), patch.object(cli, 'request') as req:
            result = cli.create_cards(CFG, [card], Path(tmp), False)
            self.assertEqual(result['cards'][0]['action'], 'create')
            req.assert_not_called()
            self.assertFalse((Path(tmp) / 'Implementation').exists())

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test'})
    def test_resume_does_not_duplicate_parent_or_children(self):
        card = cli.parse_cards(ROOT)[0]
        task = {'id': '1', 'custom_id': 'DEV-42', 'name': card['name'], 'list': {'id': CFG['list_id']}}
        tasks = [task] + [{'id': str(i+2), 'parent': '1', 'name': name} for i, name in enumerate(card['subtasks'])]
        with tempfile.TemporaryDirectory() as tmp, patch.object(cli, 'doctor'), patch.object(cli, 'all_tasks', return_value=tasks), patch.object(cli, 'request') as req:
            cli.create_cards(CFG, [card], Path(tmp), True)
            req.assert_not_called()
            binding = json.loads((Path(tmp)/'Implementation/clickup-bindings.json').read_text())
            self.assertEqual(binding[card['spec_id']]['custom_id'], 'DEV-42')

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test'})
    def test_duplicate_parent_stops(self):
        card = cli.parse_cards(ROOT)[0]
        task = {'id': '1', 'name': card['name']}
        with patch.object(cli, 'doctor'), patch.object(cli, 'all_tasks', return_value=[task, {**task, 'id':'2'}]):
            with self.assertRaisesRegex(ValueError, 'Duplicate'):
                cli.create_cards(CFG, [card], ROOT, True)

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test'})
    def test_unknown_create_outcome_is_not_retried(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(cli, 'doctor'), patch.object(cli, 'all_tasks', return_value=[]), patch.object(cli, 'request', side_effect=ValueError('unknown write')) as req:
            with self.assertRaises(ValueError):
                cli.create_cards(CFG, [cli.parse_cards(ROOT)[0]], Path(tmp), True)
            self.assertEqual(req.call_count, 1)

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test'})
    def test_pagination_empty_page_terminates(self):
        with patch.object(cli, 'request', return_value={'tasks': [], 'last_page': False}) as req:
            self.assertEqual(cli.all_tasks(CFG), [])
            self.assertEqual(req.call_count, 2)

    def test_review_signal_without_associated_pr_skips(self):
        result = cli.sync_pr(CFG, {'workflow_run': {'name': 'ClickUp review signal', 'conclusion': 'success', 'pull_requests': []}})
        self.assertEqual(result['action'], 'skip')

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test', 'GITHUB_TOKEN': 'test'})
    def test_review_signal_refetches_current_pr(self):
        event = {'workflow_run': {'name': 'ClickUp review signal', 'conclusion': 'success', 'pull_requests': [{'number': 9}]}}
        with patch.object(cli, 'request', side_effect=self.sync_response(status='ready')) as req:
            result = cli.sync_pr(CFG, event)
            self.assertEqual(result['to'], 'in review')
            self.assertTrue(any('/pulls/9' in c.args[1] for c in req.call_args_list))
            self.assertFalse(any(c.args[0] == 'PUT' for c in req.call_args_list))

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test'})
    def test_custom_id_query_and_list_scope(self):
        with patch.object(cli, 'request', return_value={'list': {'id': 'other'}}) as req:
            with self.assertRaisesRegex(ValueError, 'another List'):
                cli.get_task(CFG, 'DEV-42')
            self.assertIn('custom_task_ids=true', req.call_args.args[1])
            self.assertIn('team_id=' + CFG['workspace_id'], req.call_args.args[1])

    def test_only_imports_selected_card(self):
        with patch.object(cli, 'create_cards', return_value={}) as create, patch('builtins.print'):
            cli.main(['--root', str(ROOT), 'create-cards', '--only', 'APP-020'])
            self.assertEqual([c['spec_id'] for c in create.call_args.args[1]], ['APP-020'])

    def test_unknown_only_does_not_call_api(self):
        with patch.object(cli, 'request') as req:
            with self.assertRaisesRegex(ValueError, 'Requested spec ID'):
                cli.main(['--root', str(ROOT), 'create-cards', '--only', 'APP-999999'])
            req.assert_not_called()

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test'})
    def test_creation_validation_only_requires_backlog(self):
        listing = {'id': CFG['list_id'], 'name': 'mobilebots', 'statuses': [{'status': 'backlog'}]}
        with patch.object(cli, 'api_list', return_value=listing):
            self.assertEqual(cli.doctor(CFG, {'backlog'})['list_name'], 'mobilebots')
            with self.assertRaisesRegex(ValueError, 'configured statuses'):
                cli.doctor(CFG)

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test'})
    def test_inspect_identity_is_read_only(self):
        task = {'id': 'abc', 'name': '[APP-020] Example', 'custom_id': 'DEV-123', 'list': {'id': CFG['list_id']}, 'status': {'status': 'backlog'}, 'subtasks': [{}, {}]}
        listing = {'id': CFG['list_id'], 'name': 'mobilebots', 'space': {'id': 'space1'}}
        space = {'name': 'autobots', 'features': {}}
        with patch.object(cli, 'request', side_effect=[listing, task, space]) as req, patch.object(cli, 'all_tasks', return_value=[task]):
            result = cli.inspect_task(CFG, 'abc')
            self.assertEqual(result['custom_id'], 'DEV-123')
            self.assertEqual(result['subtask_count'], 2)
            self.assertTrue(all(call.args[0] == 'GET' for call in req.call_args_list))

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test'})
    def test_inspect_identity_falls_back_to_list(self):
        task = {'id': 'abc', 'name': '[APP-020] Example', 'custom_id': None, 'list': {'id': CFG['list_id']}, 'status': {'status': 'backlog'}}
        listing = {'id': CFG['list_id'], 'name': 'mobilebots', 'space': {'id': 'space1'}}
        with patch.object(cli, 'request', side_effect=[listing, ValueError('HTTP 401'), ValueError('HTTP 401')]), patch.object(cli, 'all_tasks', return_value=[task, {'id': 'child', 'parent': 'abc'}]):
            result = cli.inspect_task(CFG, 'abc')
            self.assertEqual(result['identity_source'], 'Get Tasks list fallback')
            self.assertIsNone(result['custom_id'])
            self.assertEqual(result['subtask_count'], 1)

    def estimate_fixture(self):
        card = next(c for c in cli.parse_cards(ROOT) if c['spec_id'] == 'APP-020')
        parent = {'id': 'parent', 'name': card['name'], 'custom_id': 'DEV-102', 'time_estimate': 14400000}
        children = [{'id': str(i), 'parent': 'parent', 'name': name, 'time_estimate': None} for i, name in enumerate(card['subtasks'])]
        return card, parent, children

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test'})
    def test_estimates_preview_is_read_only(self):
        card, parent, children = self.estimate_fixture()
        with patch.object(cli, 'all_tasks', return_value=[parent, *children]), patch.object(cli, 'request') as req:
            result = cli.set_subtask_estimates(CFG, card)
            req.assert_not_called()
            self.assertEqual(sum(u['time_estimate'] for u in result['subtasks']), 14400000)

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test'})
    def test_estimates_only_changes_time_and_clears_parent_last(self):
        card, parent, children = self.estimate_fixture()
        state = {t['id']: copy.deepcopy(t) for t in [parent, *children]}
        writes = []
        def request(method, path, token, body=None):
            ident = path.rsplit('/', 1)[1]
            if method == 'PUT':
                self.assertEqual(set(body), {'time_estimate'})
                writes.append(ident); state[ident].update(body)
            return state[ident]
        with patch.object(cli, 'all_tasks', return_value=[parent, *children]), patch.object(cli, 'request', side_effect=request):
            result = cli.set_subtask_estimates(CFG, card, True)
        self.assertEqual(writes[-1], 'parent')
        self.assertEqual(result['parent_own_estimate_ms'], 0)
        self.assertEqual(sum(state[t['id']]['time_estimate'] for t in children), 14400000)

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test'})
    def test_estimate_verification_failure_preserves_parent(self):
        card, parent, children = self.estimate_fixture()
        with patch.object(cli, 'all_tasks', return_value=[parent, *children]), patch.object(cli, 'request', return_value={'time_estimate': 0}) as req:
            with self.assertRaisesRegex(ValueError, 'parent estimate preserved'):
                cli.set_subtask_estimates(CFG, card, True)
            self.assertFalse(any(c.args[0] == 'PUT' and c.args[1].endswith('/parent') for c in req.call_args_list))

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test'})
    def test_extra_remote_subtask_prevents_estimate_writes(self):
        card, parent, children = self.estimate_fixture()
        children.append({'id': 'other', 'parent': 'parent', 'name': 'extra'})
        with patch.object(cli, 'all_tasks', return_value=[parent, *children]), patch.object(cli, 'request') as req:
            with self.assertRaisesRegex(ValueError, 'count differs'):
                cli.set_subtask_estimates(CFG, card, True)
            req.assert_not_called()

    @patch.dict(os.environ, {'CLICKUP_API_TOKEN': 'test'})
    def test_new_card_estimates_live_only_on_subtasks(self):
        card, _, _ = self.estimate_fixture()
        def request(method, path, token, body):
            return dict(body, id='child' + str(body.get('time_estimate')) if body.get('parent') else 'parent', list={'id': CFG['list_id']})
        with tempfile.TemporaryDirectory() as tmp, patch.object(cli, 'doctor'), patch.object(cli, 'all_tasks', return_value=[]), patch.object(cli, 'request', side_effect=request) as req:
            cli.create_cards(CFG, [card], Path(tmp), True)
            bodies = [c.args[3] for c in req.call_args_list]
            self.assertEqual(bodies[0]['time_estimate'], 0)
            self.assertEqual(sum(b['time_estimate'] for b in bodies[1:]), card['time_estimate'])

if __name__ == '__main__':
    unittest.main()
