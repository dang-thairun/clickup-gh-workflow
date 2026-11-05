import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

CODE_ROOT = Path(__file__).resolve().parents[1]
ROOT = CODE_ROOT / 'tests/fixtures/project'
PACKAGE = CODE_ROOT / 'src/clickup_gh_workflow'
def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result
runner = load('global_runner', PACKAGE / 'run.py')
worker = load('global_worker', PACKAGE / 'worker.py')
installer = load('global_installer', CODE_ROOT / 'scripts/install.py')
CFG = runner.tomllib.loads((ROOT/'.clickup-workflow.toml').read_text())

class GlobalWorkflowTests(unittest.TestCase):
    def test_rejects_shell_injection_before_github_access(self):
        with self.assertRaises(ValueError):
            runner.validate(CFG, 'create-card', 'APP-020; echo bad', True)
        bad = dict(CFG, fork="owner/repo' || true")
        with self.assertRaises(ValueError):
            runner.validate(bad, 'doctor', None, False)

    def test_read_operation_cannot_apply(self):
        with self.assertRaises(ValueError):
            worker.execute(Mock(), CFG, ROOT, 'read-card', 'APP-020', True)

    def test_worker_restricts_import_to_one_card(self):
        engine = Mock()
        engine.parse_cards.return_value = [{'spec_id':'APP-010'}, {'spec_id':'APP-020'}]
        engine.create_cards.return_value = {'cards':[]}
        worker.execute(engine, CFG, ROOT, 'create-card', 'APP-020')
        self.assertEqual(engine.create_cards.call_args.args[1], [{'spec_id':'APP-020'}])
        with self.assertRaises(ValueError):
            worker.execute(engine, CFG, ROOT, 'create-card', 'APP-999')

    def test_prepare_only_has_no_network_or_git_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'.clickup-workflow.toml').write_text((ROOT/'.clickup-workflow.toml').read_text())
            with patch.object(runner, 'command') as command, patch.object(runner, 'gh_json') as gh:
                path, receipt = runner.prepare(root, 'read-card', 'APP-020', False, True)
                command.assert_not_called(); gh.assert_not_called()
                payload=Path(receipt['staging'])/'.team-workflow-runs'/receipt['request_id']
                request=json.loads((payload/'request.json').read_text())
                self.assertEqual(request['id'],'APP-020')
                self.assertFalse(request['apply'])
                self.assertEqual(json.loads(path.read_text())['state'],'prepared')
                self.assertFalse((root/'.git').exists())

    def test_global_install_runs_without_codex_or_plugin_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix=Path(tmp)/'prefix'; skill=Path(tmp)/'skill'
            installer.install(prefix,skill)
            self.assertFalse(prefix.exists())
            first=installer.install(prefix,skill,True)
            installer.install(prefix,skill,True)
            result=subprocess.run([first['command'],'--root',str(ROOT),'plan'],text=True,capture_output=True,check=True)
            data=json.loads(result.stdout)
            self.assertEqual(len(data['cards']),2)
            self.assertTrue((prefix/'share/clickup-gh-workflow/engine.py').exists())
            self.assertFalse((prefix/'.codex').exists())

    def test_installer_does_not_overwrite_an_unrelated_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix=Path(tmp); entry=prefix/'bin/clickup-gh-workflow'
            entry.parent.mkdir(); entry.write_text('unrelated')
            with self.assertRaises(ValueError):
                installer.install(prefix,prefix/'skill',True)
            self.assertEqual(entry.read_text(),'unrelated')

    def test_resume_reuses_result_instead_of_replaying_or_downloading(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'receipt.json'; project=Path(tmp)/'project'; project.mkdir()
            receipt={'repository':CFG['fork'],'run_id':123,'project':str(project),'operation':'doctor','artifact':'result'}
            destination=path.with_suffix(''); destination.mkdir()
            (destination/'result.json').write_text(json.dumps({'success':True,'result':{'list_name':'mobilebots'}}))
            with patch.object(runner,'gh_json',return_value={'status':'completed','conclusion':'success'}), patch.object(runner,'command') as command, patch('builtins.print'):
                runner.finish(path,receipt,10)
                command.assert_not_called()
            self.assertEqual(json.loads(path.read_text())['state'],'completed')

if __name__=='__main__':
    unittest.main()
