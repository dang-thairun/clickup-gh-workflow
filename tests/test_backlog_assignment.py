import copy
import importlib.util
import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

CODE_ROOT = Path(__file__).resolve().parents[1]
ROOT = CODE_ROOT / 'tests/fixtures/project'
PACKAGE = CODE_ROOT / 'src/clickup_gh_workflow'
spec = importlib.util.spec_from_file_location('assignment_ops', PACKAGE / 'operations.py')
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'Implementation').mkdir()
        (self.root / 'Implementation/clickup-bindings.json').write_text(json.dumps({'APP-020': {'id': 'p'}}))
        self.cfg = {'list_id': '123'}
        self.engine = Mock()
        self.engine.request.return_value = {'members': [{'id': 77, 'username': 'target'}]}
        self.engine.parse_cards.return_value = [{'spec_id': 'APP-020', 'subtasks': ['Child']}]
        self.tasks = [
            {'id': 'p', 'name': '[APP-020] Parent', 'list': {'id': '123'}, 'assignees': [{'id': 99}]},
            {'id': 'c', 'name': 'Child', 'parent': 'p', 'list': {'id': '123'}, 'assignees': []},
            {'id': 'unrelated', 'name': 'Other task', 'list': {'id': '123'}, 'assignees': []},
        ]
        self.engine.all_tasks.return_value = self.tasks

    def test_preview_never_writes_and_excludes_unrelated_tasks(self):
        result = ops.assign_backlog(self.engine, self.cfg, self.root, {'assignee': 77}, False)
        self.assertEqual((result['parents'], result['subtasks'], result['changes']), (1, 1, 2))
        self.assertEqual({t['id'] for t in result['tasks']}, {'p', 'c'})
        self.assertTrue(all(c.args[0] == 'GET' for c in self.engine.request.call_args_list))

    def test_unknown_member_blocks_before_any_write(self):
        with self.assertRaisesRegex(ValueError, 'verified List member'):
            ops.assign_backlog(self.engine, self.cfg, self.root, {'assignee': 88}, True)
        self.assertTrue(all(c.args[0] == 'GET' for c in self.engine.request.call_args_list))

    def test_missing_child_blocks_entire_batch_before_write(self):
        self.engine.all_tasks.return_value = [self.tasks[0]]
        with self.assertRaisesRegex(ValueError, 'Subtasks differ'):
            ops.assign_backlog(self.engine, self.cfg, self.root, {'assignee': 77}, True)
        self.assertTrue(all(c.args[0] == 'GET' for c in self.engine.request.call_args_list))

    def test_apply_adds_only_assignees_and_verifies_all_targets(self):
        after = copy.deepcopy(self.tasks)
        for t in after[:2]:
            t['assignees'].append({'id': 77})
        self.engine.all_tasks.side_effect = [self.tasks, after]
        with patch('time.sleep'):
            result = ops.assign_backlog(self.engine, self.cfg, self.root, {'assignee': 77}, True)
        writes = [c for c in self.engine.request.call_args_list if c.args[0] == 'PUT']
        self.assertEqual(len(writes), 2)
        self.assertTrue(all(c.args[3] == {'assignees': {'add': [77], 'rem': []}} for c in writes))
        self.assertTrue(result['verified'])

    def test_repeat_skips_already_assigned_tasks(self):
        for t in self.tasks[:2]:
            t['assignees'].append({'id': 77})
        result = ops.assign_backlog(self.engine, self.cfg, self.root, {'assignee': 77}, True)
        self.assertEqual(result['changes'], 0)
        self.assertTrue(result['verified'])
        self.assertTrue(all(c.args[0] == 'GET' for c in self.engine.request.call_args_list))


class GroupingTests(unittest.TestCase):
    def setUp(self):
        AssignmentTests.setUp(self)
        (self.root / 'Implementation/clickup-bindings.json').write_text(json.dumps({'APP-020': {'id':'p','custom_id':'DEV-1','url':'https://example.com/p'}}))
        self.cfg.update(statuses={'backlog':'backlog'}, groups={'operator':{'name':'Example feature group','cards':['APP-020']}})
        self.state = {t['id']: copy.deepcopy(t) for t in self.tasks}
        for t in self.state.values():
            t.update(status={'status':'backlog'}, priority={'id':'3'}, time_estimate=0)
        self.state['c']['time_estimate'] = 3600000
        self.engine.all_tasks.side_effect = lambda cfg: copy.deepcopy(list(self.state.values()))
        self.engine.status_name.side_effect = lambda t: t['status']['status']
        self.normalize_markdown=False
        def request(method,path,token,body=None):
            if path.endswith('/member'):
                return {'members':[{'id':77,'username':'target'}]}
            if method == 'POST' and '/field/' in path:
                self.state['g']['custom_fields'][0]['value']=body['value']
                return {}
            if method == 'POST':
                self.state['g'] = dict(body,id='g',custom_id='DEV-99',url='https://example.com/g',list={'id':'123'},markdown_description=body['markdown_content'],custom_fields=[{'id':'link','name':'PR Link','type':'url'}])
                return copy.deepcopy(self.state['g'])
            tid = path.split('?')[0].rsplit('/',1)[1]
            if method == 'PUT':
                self.state[tid].update(body)
                if 'markdown_content' in body:self.state[tid]['markdown_description']=body['markdown_content']
            result=copy.deepcopy(self.state[tid])
            if self.normalize_markdown and 'markdown_description' in result:
                result['markdown_description']=re.sub(r'\[(clickup-gh-workflow:[^\]]+)\]',r'\\[\1\\]',result['markdown_description'])
                result['markdown_description']=result['markdown_description'].replace('Draft PR: https://github.com/org/repo/pull/14','Draft PR: [https://github.com/org/repo/pull/14](https://github.com/org/repo/pull/14)')
            return result
        self.engine.request.side_effect = request

    def test_group_preview_never_writes(self):
        result=ops.group_backlog(self.engine,self.cfg,self.root,{'assignee':77},False)
        self.assertEqual((result['moves'],result['delivery_units'],result['step_subtasks']),(1,1,1))
        self.assertTrue(all(c.args[0]=='GET' for c in self.engine.request.call_args_list))

    def test_group_preserves_ids_steps_estimates_and_repeat_is_noop(self):
        with patch('time.sleep'):
            first=ops.group_backlog(self.engine,self.cfg,self.root,{'assignee':77},True)
            second=ops.group_backlog(self.engine,self.cfg,self.root,{'assignee':77},True)
        self.assertTrue(first['verified'] and second['verified'])
        self.assertEqual(second['moves'],0)
        self.assertEqual(self.state['p']['parent'],'g')
        self.assertEqual(self.state['c']['parent'],'p')
        self.assertEqual(self.state['c']['time_estimate'],3600000)
        self.assertEqual(self.state['p']['assignees'],[{'id':99}])
        self.assertEqual(sum(c.args[0]=='POST' for c in self.engine.request.call_args_list),1)

    def test_foreign_existing_parent_blocks_before_creation(self):
        self.state['p']['parent']='foreign'
        with self.assertRaisesRegex(ValueError,'Unexpected delivery unit'):
            ops.group_backlog(self.engine,self.cfg,self.root,{'assignee':77},True)
        self.assertTrue(all(c.args[0]=='GET' for c in self.engine.request.call_args_list))

    def test_nested_delivery_unit_can_own_pr_but_step_cannot(self):
        with patch('time.sleep'):
            ops.group_backlog(self.engine,self.cfg,self.root,{'assignee':77},True)
        ops.verify_delivery_parent(self.engine,self.cfg,self.state['p'])
        with self.assertRaises(ValueError):
            ops.verify_delivery_parent(self.engine,self.cfg,self.state['c'])

    def test_group_pr_link_preserves_notes_and_existing_child_ids(self):
        self.cfg['repository']='org/repo'
        with patch('time.sleep'):
            ops.group_backlog(self.engine,self.cfg,self.root,{'assignee':77},True)
        self.state['g']['markdown_description']+='\nManual note to preserve'
        record={'id':'g','verified':True,'pr_url':'https://github.com/org/repo/pull/14','branch':'feat/DEV-99-group'}
        result=ops.group_backlog(self.engine,self.cfg,self.root,{'assignee':77,'prs':{'operator':record}},True)
        self.assertTrue(result['pr_links_verified'])
        self.assertEqual(result['moves'],0)
        self.assertIn('Manual note to preserve',self.state['g']['markdown_description'])
        self.assertEqual(self.state['g']['custom_fields'][0]['value'],record['pr_url'])
        self.assertEqual(self.state['c']['parent'],'p')

    def test_group_pr_record_for_other_task_is_rejected_before_write(self):
        self.cfg['repository']='org/repo'
        with patch('time.sleep'):
            ops.group_backlog(self.engine,self.cfg,self.root,{'assignee':77},True)
        self.engine.request.reset_mock()
        record={'id':'foreign','verified':True,'pr_url':'https://github.com/org/repo/pull/14','branch':'feat/DEV-99-group'}
        with self.assertRaisesRegex(ValueError,'does not match group'):
            ops.group_backlog(self.engine,self.cfg,self.root,{'assignee':77,'prs':{'operator':record}},True)
        self.assertTrue(all(c.args[0]=='GET' for c in self.engine.request.call_args_list))

    def test_clickup_markdown_normalization_does_not_break_link_verification(self):
        self.cfg['repository']='org/repo'
        with patch('time.sleep'):
            ops.group_backlog(self.engine,self.cfg,self.root,{'assignee':77},True)
        self.normalize_markdown=True
        record={'id':'g','verified':True,'pr_url':'https://github.com/org/repo/pull/14','branch':'feat/DEV-99-group'}
        first=ops.group_backlog(self.engine,self.cfg,self.root,{'assignee':77,'prs':{'operator':record}},True)
        second=ops.group_backlog(self.engine,self.cfg,self.root,{'assignee':77,'prs':{'operator':record}},True)
        self.assertTrue(first['pr_links_verified'] and second['pr_links_verified'])
        self.assertEqual(ops.description(self.state['g']).count('[clickup-gh-workflow:group-pr-begin]'),1)


if __name__ == '__main__':
    unittest.main()
