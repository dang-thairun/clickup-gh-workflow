import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

CODE_ROOT = Path(__file__).resolve().parents[1]
ROOT = CODE_ROOT / 'tests/fixtures/project'
PACKAGE = CODE_ROOT / 'src/clickup_gh_workflow'
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result
ops=load('operations_test',PACKAGE / 'operations.py')
engine=load('engine_ops_test',PACKAGE / 'engine.py')
CFG=engine.tomllib.loads((ROOT/'.clickup-workflow.toml').read_text())

class OperationsTests(unittest.TestCase):
    def remote(self):
        return {'id':'parent','custom_id':'DEV-102','name':'[APP-020] Example feature','list':{'id':CFG['list_id']},
                'status':{'status':'backlog'},'description':'Human notes','time_estimate':0,'custom_fields':[]}
    def api(self):
        e=Mock(wraps=engine);e.request=Mock(return_value={});e.TASK_ID=engine.TASK_ID;e.CUSTOM_ID=engine.CUSTOM_ID;e.token.return_value='test'
        return e

    def test_duration_is_explicit_and_positive(self):
        self.assertEqual(ops.duration('1h30m'),5400000)
        for value in ('90','-1h','1hjunk','0m'):
            with self.assertRaises(ValueError):ops.duration(value)

    def test_dates_require_timezone(self):
        self.assertEqual(ops.timestamp('2026-11-06T17:00:00+07:00'),ops.timestamp('2026-11-06T10:00:00Z'))
        with self.assertRaises(ValueError):ops.timestamp('2026-11-06T17:00:00')

    def test_update_preview_never_writes(self):
        e=self.api();result=ops.update(e,CFG,self.remote(),{'priority':'high','due':'2026-11-06T17:00:00+07:00'},False)
        e.request.assert_not_called();self.assertEqual(result['changes']['priority'],2)
        self.assertNotIn('status',result['changes'])

    def test_due_before_start_is_rejected(self):
        e=self.api()
        with self.assertRaises(ValueError):ops.update(e,CFG,self.remote(),{'planned_start':'2026-11-07T09:00:00+07:00','due':'2026-11-06T17:00:00+07:00'},True)
        e.request.assert_not_called()

    def test_rename_keeps_spec_identity(self):
        e=self.api();result=ops.update(e,CFG,self.remote(),{'name':'Better feature'},True)
        self.assertEqual(result['changes']['name'],'[APP-020] Better feature')
        with self.assertRaises(ValueError):ops.update(e,CFG,self.remote(),{'name':'[APP-999] Other'},True)

    def test_unknown_status_is_not_created(self):
        e=self.api();e.api_list.return_value={'statuses':[{'status':'backlog'},{'status':'ready'}]}
        with self.assertRaisesRegex(ValueError,'does not exist'):ops.set_status(e,CFG,self.remote(),{'to':'rework'},True)
        e.request.assert_not_called()

    def test_custom_dropdown_maps_label_to_id(self):
        e=self.api();remote=self.remote();remote['custom_fields']=[{'id':'f','name':'Type','type':'drop_down','type_config':{'options':[{'id':'choice','name':'Feature'}]}}]
        ops.update(e,CFG,remote,{'fields':['Type=Feature']},True)
        self.assertEqual(e.request.call_args.args[3],{'value':'choice'})

    def test_parent_estimate_cannot_double_count_children(self):
        e=self.api();e.all_tasks.return_value=[{'id':'child','parent':'parent'}]
        with self.assertRaisesRegex(ValueError,'zero own estimate'):ops.estimate(e,CFG,ROOT,self.remote(),{'duration_ms':3600000},True)
        e.request.assert_not_called()

    def test_sync_requires_adoption_and_fresh_baseline(self):
        e=self.api();e.parse_cards.return_value=[{'spec_id':'APP-020','name':'[APP-020] New','description':'Spec body','spec':'Implementation/test.md','subtasks':[],'subtask_estimates_ms':[]}];e.all_tasks.return_value=[]
        remote=self.remote()
        with self.assertRaisesRegex(ValueError,'Legacy'):ops.sync(e,CFG,ROOT,remote,{},True)
        with self.assertRaisesRegex(ValueError,'changed'):ops.sync(e,CFG,ROOT,remote,{'adopt':True,'expected_remote_sha':'stale'},True)
        e.request.assert_not_called()

    def test_sync_preserves_manual_notes_and_runtime_fields(self):
        e=self.api();e.parse_cards.return_value=[{'spec_id':'APP-020','name':'[APP-020] New','description':'New body','spec':'Implementation/test.md','subtasks':[],'subtask_estimates_ms':[]}];e.all_tasks.return_value=[]
        remote=self.remote();remote['markdown_description']='Notes before\n'+ops.BEGIN+'\nOld body\n'+ops.END+'\nNotes after'
        baseline=ops.diff(e,ROOT,remote)['remote_sha']
        refreshed=dict(remote,markdown_description=remote['markdown_description'].replace('Old body','New body'))
        with patch.object(ops,'task',return_value=refreshed):ops.sync(e,CFG,ROOT,remote,{'expected_remote_sha':baseline},True)
        payload=e.request.call_args.args[3]
        self.assertEqual(set(payload),{'name','markdown_content'})
        self.assertIn('Notes before',payload['markdown_content']);self.assertIn('Notes after',payload['markdown_content'])

    def test_time_log_reuses_request_marker(self):
        e=self.api();key='12345678-1234';e.request.return_value={'data':[{'id':'existing','description':'[clickup-gh-workflow:'+key+']'}]}
        result=ops.time_entries(e,CFG,self.remote(),{'start_ms':1,'duration_ms':1000,'request_id':key},True)
        self.assertEqual(result['action'],'existing');self.assertEqual(e.request.call_count,1)
        self.assertEqual(e.request.call_args.args[0],'GET')

    def test_time_log_only_posts_elapsed_time(self):
        e=self.api();e.request.side_effect=[{'data':[]},{'data':{'id':'new'}}]
        ops.time_entries(e,CFG,self.remote(),{'start_ms':1000,'duration_ms':5400000,'request_id':'abcdef-1234','note':'tests'},True)
        payload=e.request.call_args.args[3]
        self.assertEqual(payload['start'],1000);self.assertEqual(payload['duration'],5400000)
        self.assertNotIn('end',payload)

    def test_pr_metadata_never_changes_schedule_or_marks_merged_done(self):
        e=self.api();remote=self.remote();remote['custom_fields']=[{'id':'url','name':'PR Link','type':'url'},{'id':'started','name':'Start Date','type':'date'},{'id':'merged','name':'End Date','type':'date'}]
        result=ops.pr_metadata(e,CFG,remote,{'url':'https://github.com/'+CFG['repository']+'/pull/99','base':'develop','created_at':'2026-11-04T01:00:00Z','merged_at':'2026-11-05T01:00:00Z','draft':False,'state':'MERGED'},True)
        self.assertIsNone(result['status'])
        self.assertTrue(all(c.args[0]=='POST' and '/field/' in c.args[1] for c in e.request.call_args_list))
        self.assertNotIn('due_date',json.dumps([c.args for c in e.request.call_args_list]))

    def test_time_entry_delete_cannot_target_another_task(self):
        e=self.api();e.request.return_value={'data':{'id':'123','task':{'id':'another'}}}
        with self.assertRaisesRegex(ValueError,'selected task'):
            ops.time_entries(e,CFG,self.remote(),{'mode':'delete','entry_id':'123','reason':'duplicate'},True)
        self.assertEqual(e.request.call_count,1);self.assertEqual(e.request.call_args.args[0],'GET')

    def test_edit_time_entry_keeps_retry_marker_and_existing_tags(self):
        e=self.api();e.request.return_value={'data':{'id':'123','task':{'id':'parent'},'description':'old [clickup-gh-workflow:abc-123]','duration':'1000'}}
        ops.time_entries(e,CFG,self.remote(),{'mode':'edit','entry_id':'123','duration_ms':1800000,'note':'corrected'},True)
        payload=e.request.call_args.args[3]
        self.assertIn('[clickup-gh-workflow:abc-123]',payload['description'])
        self.assertEqual(payload['tag_action'],'add');self.assertEqual(payload['tags'],[])
        self.assertEqual(payload['duration'],1800000)

    def test_bulk_entry_deletion_is_rejected(self):
        e=self.api()
        with self.assertRaisesRegex(ValueError,'single numeric'):
            ops.time_entries(e,CFG,self.remote(),{'mode':'delete','entry_id':'1,2','reason':'duplicate'},True)
        e.request.assert_not_called()

    def test_time_retries_filter_the_selected_assignee(self):
        e=self.api();e.request.return_value={'data':[]}
        ops.time_entries(e,CFG,self.remote(),{'start_ms':1,'duration_ms':1000,'request_id':'abcd-1234','assignee':42},False)
        self.assertIn('assignee=42',e.request.call_args.args[1])

    def test_start_requires_ready_and_does_not_make_subtask_branches(self):
        e=self.api()
        with self.assertRaisesRegex(ValueError,'READY'):ops.resolve_start(e,CFG,ROOT,self.remote())
        remote=self.remote();remote['parent']='other'
        with self.assertRaisesRegex(ValueError,'parent card'):ops.resolve_start(e,CFG,ROOT,remote)

if __name__=='__main__':unittest.main()
