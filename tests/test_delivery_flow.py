import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

CODE_ROOT = Path(__file__).resolve().parents[1]
ROOT = CODE_ROOT / 'tests/fixtures/project'
PACKAGE = CODE_ROOT / 'src/clickup_gh_workflow'
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
d=load('delivery_test',PACKAGE / 'delivery.py')
f=load('frontend_test',PACKAGE / 'frontend.py')
CFG={'repository':'owner/upstream','fork':'me/fork','base_branch':'develop','statuses':{'ready':'ready'}}

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        def git(*args):
            return subprocess.run(['git',*args],cwd=self.root,check=True,capture_output=True,text=True).stdout.strip()
        self.git=git;git('init','-q');git('config','user.name','Test');git('config','user.email','test@example.invalid')
        (self.root/'src').mkdir();(self.root/'src/app.py').write_text('print("base")\n')
        git('add','.');git('commit','-qm','base');base=git('rev-parse','HEAD');git('switch','-qc','feat/DEV-1-example')
        self.card={'spec_id':'APP-010','name':'[APP-010] Example','spec':'Implementation/test.md','description':'**Owned Paths:** `src/app.py`\n**Context & Scope:** Example'}
        self.state={'branch':'feat/DEV-1-example','base_sha':base,'spec_id':'APP-010','spec':'Implementation/test.md','checks':[]}
        d.save(self.root,'DEV-1',self.state)
        e=Mock();e.parse_cards.return_value=[self.card]
        self.runtime={'engine':lambda:e,'command':Mock(return_value='[]')}
    def args(self,phase='red',**extra):
        values={'phase':phase,'reason':'expected business assertion','command':[sys.executable,'src/app.py'],'timeout':10,'label':'test'};values.update(extra);return SimpleNamespace(**values)
    def red(self):
        (self.root/'src/app.py').write_text('raise AssertionError("expected business condition")\n')
        return d.check(self.runtime,CFG,self.root,'DEV-1',self.args())

    def test_captures_real_red_and_requires_explicit_approval(self):
        result=self.red();self.assertEqual(result['exit_code'],1);self.assertTrue((self.root/result['log']).exists())
        with self.assertRaisesRegex(ValueError,'approved'):d.check(self.runtime,CFG,self.root,'DEV-1',self.args('green'))
        d.approve_red(self.root,'DEV-1',SimpleNamespace(reason='Verified assertion is the intended business failure',apply=True))
        self.assertIn('red_approval',d.load(self.root,'DEV-1'))

    def test_missing_executable_does_not_count_as_red(self):
        with self.assertRaisesRegex(ValueError,'does not satisfy'):
            d.check(self.runtime,CFG,self.root,'DEV-1',self.args(command=['missing-executable-987654']))
        self.assertFalse(d.load(self.root,'DEV-1')['checks'][-1]['valid'])

    def test_rejects_changes_outside_owned_paths_before_executing_tests(self):
        (self.root/'unrelated.txt').write_text('other work')
        with self.assertRaisesRegex(ValueError,'outside'):d.check(self.runtime,CFG,self.root,'DEV-1',self.args())
        self.assertEqual(d.load(self.root,'DEV-1')['checks'],[])

    def test_green_evidence_becomes_stale_after_source_change(self):
        self.red();d.approve_red(self.root,'DEV-1',SimpleNamespace(reason='Verified',apply=True))
        (self.root/'src/app.py').write_text('print("passes")\n')
        d.check(self.runtime,CFG,self.root,'DEV-1',self.args('green'))
        state=d.load(self.root,'DEV-1');self.assertEqual(len(d.green_gate(self.runtime,CFG,self.root,'DEV-1',state)),1)
        (self.root/'src/app.py').write_text('print("changed after test")\n')
        with self.assertRaisesRegex(ValueError,'stale'):d.green_gate(self.runtime,CFG,self.root,'DEV-1',state)

    def test_commit_does_not_invalidate_identical_red_content(self):
        self.red();before=d.fingerprint(self.root,self.state['base_sha'])
        self.git('add','src/app.py');self.git('commit','-qm','test: RED\n\nClickUp-Task: DEV-1')
        self.assertEqual(before,d.fingerprint(self.root,self.state['base_sha']))
        d.commit_gate(self.root,'DEV-1',self.state)

    def test_commit_without_task_trailer_is_rejected(self):
        self.red();self.git('add','src/app.py');self.git('commit','-qm','missing trailer')
        with self.assertRaisesRegex(ValueError,'ClickUp-Task'):d.commit_gate(self.root,'DEV-1',self.state)

    def test_pr_open_is_draft_and_binds_metadata_after_recording_url(self):
        self.red();self.git('add','src/app.py');self.git('commit','-qm','test: RED\n\nClickUp-Task: DEV-1')
        pr={'url':'https://github.com/owner/upstream/pull/1','baseRefName':'develop','createdAt':'2026-11-04T01:00:00Z','mergedAt':None,'isDraft':True,'state':'OPEN'}
        rpc=Mock(return_value={});real_git=d.git
        def git(root,*args):return '' if args[0]=='push' else real_git(root,*args)
        with patch.object(d,'find_pr',side_effect=[None,pr]),patch.object(d,'git',side_effect=git):
            result=d.pr_command(self.runtime,rpc,CFG,self.root,'DEV-1',SimpleNamespace(action='open',apply=True))
        call=self.runtime['command'].call_args.args[0]
        self.assertIn('--draft',call);self.assertIn('--body-file',call)
        body=Path(call[call.index('--body-file')+1]).read_text();self.assertTrue(body.startswith('ClickUp-Task: DEV-1\n'))
        self.assertEqual(d.load(self.root,'DEV-1')['pr_url'],pr['url'])
        self.assertIn('STOP',result['next']);self.assertEqual(rpc.call_args.args[0],'pr-metadata')

    def test_start_uses_approved_base_and_adds_only_runtime_state(self):
        (self.root/'.clickup-workflow.toml').write_text('base_branch="develop"\n')
        (self.root/'Implementation').mkdir();(self.root/'Implementation/test-spec.md').write_text('# Approved plan\n')
        self.git('add','.clickup-workflow.toml','Implementation/test-spec.md');self.git('commit','-qm','approved planning')
        base=self.git('rev-parse','HEAD');real_git=d.git
        remote={'id':'task2','custom_id':'DEV-2','url':'https://app.clickup.com/t/task2','spec_id':'APP-020','name':'[APP-020] Example','spec':'Implementation/test-spec.md','dependencies':[]}
        def git(root,*args):
            if args[0]=='fetch':return ''
            if args==('rev-parse','FETCH_HEAD'):return base
            return real_git(root,*args)
        with patch.object(d,'find_pr',return_value=None),patch.object(d,'git',side_effect=git):
            result=d.start(self.runtime,Mock(return_value=remote),CFG,self.root,'DEV-2',SimpleNamespace(type='feat',apply=True))
        self.assertEqual(self.git('branch','--show-current'),'feat/DEV-2-example')
        self.assertEqual(result['base_sha'],base);self.assertEqual(d.changed(self.root,base),[])
        self.assertFalse((self.root/'Implementation/clickup-bindings.json').exists())

    def test_start_stops_before_switch_when_base_has_no_planning(self):
        real_git=d.git;old=self.git('branch','--show-current')
        remote={'id':'task2','custom_id':'DEV-2','spec_id':'APP-020','name':'[APP-020] Example','spec':'Implementation/test-spec.md','dependencies':[]}
        def git(root,*args):
            if args[0]=='fetch':return ''
            if args==('rev-parse','FETCH_HEAD'):return self.state['base_sha']
            return real_git(root,*args)
        with patch.object(d,'find_pr',return_value=None),patch.object(d,'git',side_effect=git):
            with self.assertRaisesRegex(ValueError,'approved planning'):
                d.start(self.runtime,Mock(return_value=remote),CFG,self.root,'DEV-2',SimpleNamespace(type='feat',apply=True))
        self.assertEqual(self.git('branch','--show-current'),old)

    def test_ci_requires_success_or_explicit_no_ci_policy(self):
        with self.assertRaisesRegex(ValueError,'No CI'):d.ci_gate(CFG,{'statusCheckRollup':[]})
        d.ci_gate(dict(CFG,delivery={'allow_no_ci':True}),{'statusCheckRollup':[]})
        with self.assertRaisesRegex(ValueError,'pending'):d.ci_gate(CFG,{'statusCheckRollup':[{'status':'IN_PROGRESS','conclusion':None}]})
        d.ci_gate(CFG,{'statusCheckRollup':[{'status':'COMPLETED','conclusion':'SUCCESS'}]})

    def test_start_will_not_stash_or_reset_dirty_work(self):
        (self.root/'other.txt').write_text('preserve')
        rpc=Mock(return_value={'custom_id':'DEV-1','spec_id':'APP-010','name':'[APP-010] Example','spec':'Implementation/test.md','dependencies':[]})
        with patch.object(d,'find_pr',return_value=None):
            with self.assertRaisesRegex(ValueError,'Working tree'):d.start(self.runtime,rpc,CFG,self.root,'DEV-1',SimpleNamespace(type='feat',apply=True))
        self.assertEqual((self.root/'other.txt').read_text(),'preserve');self.runtime['command'].assert_not_called()

class FrontendTests(unittest.TestCase):
    def test_check_accepts_flags_after_id(self):
        with patch.object(f,'module') as module:
            delivery=Mock();delivery.check.return_value={};operations=Mock();module.side_effect=[operations,delivery]
            with tempfile.TemporaryDirectory() as tmp,patch('builtins.print'):
                root=Path(tmp);(root/'.clickup-workflow.toml').write_text('fork="me/fork"')
                runtime={'tomllib':__import__('tomllib')}
                f.main(['--root',str(root),'check','DEV-1','--phase','red','--reason','expected','--','python3','-m','unittest'],runtime)
                args=delivery.check.call_args.args[-1]
                self.assertEqual(args.phase,'red');self.assertEqual(args.command,['python3','-m','unittest'])

    def test_prepare_only_escapes_without_running_local_mutations(self):
        with tempfile.TemporaryDirectory() as tmp,patch('builtins.print'):
            root=Path(tmp);(root/'.clickup-workflow.toml').write_text('fork="me/fork"')
            engine=Mock();runtime={'tomllib':__import__('tomllib'),'engine':lambda:engine}
            result=f.main(['--root',str(root),'--via','local','update','DEV-1','--priority','high','--apply','--prepare-only'],runtime)
            self.assertTrue(result['prepared_only']);engine.request.assert_not_called()

    def test_clear_due_is_a_real_update_even_when_value_is_null(self):
        with tempfile.TemporaryDirectory() as tmp,patch('builtins.print'),patch.object(f,'module') as module:
            root=Path(tmp);(root/'.clickup-workflow.toml').write_text('fork="me/fork"')
            operations=Mock();delivery=Mock();worker=Mock();worker.execute.return_value={}
            module.side_effect=[operations,delivery,worker]
            f.main(['--root',str(root),'--via','local','update','DEV-1','--clear-due'],{'tomllib':__import__('tomllib'),'engine':Mock(return_value=Mock())})
            self.assertIn('due',worker.execute.call_args.args[-1]);self.assertIsNone(worker.execute.call_args.args[-1]['due'])

if __name__=='__main__':unittest.main()
