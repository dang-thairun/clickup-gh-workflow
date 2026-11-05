import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

CODE_ROOT=Path(__file__).resolve().parents[1]
PACKAGE=CODE_ROOT/'src/clickup_gh_workflow'
def load(name):
    spec=importlib.util.spec_from_file_location('portable_'+name,PACKAGE/(name+'.py'))
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result
engine=load('engine');ops=load('operations');delivery=load('delivery');runner=load('run')
CFG=engine.tomllib.loads((CODE_ROOT/'tests/fixtures/project/.clickup-workflow.toml').read_text())

class PortabilityTests(unittest.TestCase):
    def cli(self,root,*args):
        return subprocess.run([sys.executable,str(PACKAGE/'run.py'),'--root',str(root),*args],capture_output=True,text=True)

    def test_init_preview_is_offline_and_apply_creates_parseable_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'new-project'
            p=self.cli(root,'init');self.assertEqual(p.returncode,0,p.stderr)
            self.assertFalse(root.exists())
            p=self.cli(root,'init','--apply');self.assertEqual(p.returncode,0,p.stderr)
            self.assertEqual(len(engine.parse_cards(root)),2)
            p=self.cli(root,'plan');self.assertEqual(p.returncode,0,p.stderr)
            self.assertEqual(len(json.loads(p.stdout)['cards']),2)

    def test_init_never_overwrites_existing_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'.clickup-workflow.toml';path.write_text('manual settings')
            p=self.cli(root,'init','--apply');self.assertNotEqual(p.returncode,0)
            self.assertEqual(path.read_text(),'manual settings')
            self.assertFalse((root/'Implementation').exists())

    def test_skill_is_available_without_project_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=self.cli(Path(tmp),'skill');self.assertEqual(p.returncode,0,p.stderr)
            self.assertIn('name: clickup-gh-workflow',p.stdout)

    def test_custom_prefix_is_used_for_the_real_api_lookup(self):
        cfg=dict(CFG,custom_id_prefix='OPS')
        with patch.object(engine,'token',return_value='test'),patch.object(engine,'request',return_value={'id':'internal','list':{'id':cfg['list_id']}}) as request:
            result=engine.get_task(cfg,'OPS-42')
        self.assertEqual(result['id'],'internal')
        self.assertIn('/task/OPS-42?',request.call_args.args[1])
        self.assertIn('custom_task_ids=true',request.call_args.args[1])

    def test_custom_prefix_does_not_break_local_spec_resolution(self):
        cfg=dict(CFG,custom_id_prefix='OPS')
        e=Mock(wraps=engine);e.TASK_ID=engine.TASK_ID;e.token.return_value='test'
        e.all_tasks.return_value=[{'id':'internal','name':'[APP-010] Example'}]
        e.request.return_value={'id':'internal','list':{'id':cfg['list_id']}}
        result=ops.task(e,cfg,'APP-010')
        self.assertEqual(result['id'],'internal')
        self.assertIn('/task/internal?',e.request.call_args.args[1])

    def test_bad_custom_prefix_is_rejected_before_network(self):
        with patch.object(engine,'request') as request:
            with self.assertRaises(ValueError):engine.get_task(dict(CFG,custom_id_prefix='BAD;command'),'OPS-42')
        request.assert_not_called()

    def test_other_prefix_can_store_delivery_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);delivery.save(root,'OPS-42',{'branch':'feat/OPS-42-example'})
            self.assertEqual(delivery.load(root,'OPS-42')['branch'],'feat/OPS-42-example')
            with self.assertRaises(ValueError):delivery.state_path(root,'../../escape')

    def test_prefix_with_underscore_passes_transport_and_api_validation(self):
        cfg=dict(CFG,custom_id_prefix='TEAM_OPS')
        runner.validate(cfg,'read','TEAM_OPS-42',False)
        with patch.object(engine,'token',return_value='test'),patch.object(engine,'request',return_value={'id':'internal','list':{'id':cfg['list_id']}}):
            self.assertEqual(engine.get_task(cfg,'TEAM_OPS-42')['id'],'internal')

    def test_non_fibonacci_plan_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'Implementation').mkdir()
            text=(CODE_ROOT/'examples/example-spec.md').read_text().replace('Feature / 2 / 4h','Feature / 4 / 4h')
            (root/'Implementation/example-spec.md').write_text(text)
            with self.assertRaisesRegex(ValueError,'estimate'):engine.parse_cards(root)

if __name__=='__main__':unittest.main()
