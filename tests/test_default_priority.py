import copy
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

CODE_ROOT = Path(__file__).resolve().parents[1]
ROOT = CODE_ROOT / 'tests/fixtures/project'
PACKAGE = CODE_ROOT / 'src/clickup_gh_workflow'
spec=importlib.util.spec_from_file_location('priority_engine',PACKAGE / 'engine.py')
e=importlib.util.module_from_spec(spec);spec.loader.exec_module(e)
CFG=e.tomllib.loads((ROOT/'.clickup-workflow.toml').read_text())

class DefaultPriorityTests(unittest.TestCase):
    def create(self,cfg):
        card=e.parse_cards(ROOT)[0]
        bodies=[]
        def request(method,path,token,body):
            bodies.append(body)
            return dict(body,id='child'+str(len(bodies)) if body.get('parent') else 'parent',list={'id':cfg['list_id']})
        with tempfile.TemporaryDirectory() as tmp,patch.object(e,'doctor'),patch.object(e,'all_tasks',return_value=[]),patch.object(e,'token',return_value='test'),patch.object(e,'request',side_effect=request):
            e.create_cards(cfg,[card],Path(tmp),True)
        return bodies
    def test_parent_and_children_default_to_normal_without_config(self):
        cfg=copy.deepcopy(CFG);cfg.pop('defaults',None)
        bodies=self.create(cfg)
        self.assertGreater(len(bodies),1)
        self.assertTrue(all(b['priority']==3 for b in bodies))
        self.assertTrue(all(b['status']=='backlog' for b in bodies))
    def test_project_default_can_be_overridden(self):
        cfg=copy.deepcopy(CFG);cfg['defaults']={'priority':'high'}
        self.assertTrue(all(b['priority']==2 for b in self.create(cfg)))
    def test_invalid_priority_fails_before_api(self):
        cfg=copy.deepcopy(CFG);cfg['defaults']={'priority':'invalid'}
        with patch.object(e,'request') as request:
            with self.assertRaisesRegex(ValueError,'priority'):
                e.create_cards(cfg,e.parse_cards(ROOT)[:1],ROOT,True)
            request.assert_not_called()
    def test_existing_card_priority_is_preserved(self):
        card=e.parse_cards(ROOT)[0]
        parent={'id':'parent','name':card['name'],'priority':{'id':'1'},'list':{'id':CFG['list_id']}}
        tasks=[parent]+[{'id':str(i),'parent':'parent','name':name} for i,name in enumerate(card['subtasks'])]
        with tempfile.TemporaryDirectory() as tmp,patch.object(e,'doctor'),patch.object(e,'all_tasks',return_value=tasks),patch.object(e,'request') as request:
            e.create_cards(CFG,[card],Path(tmp),True)
            request.assert_not_called()
            self.assertEqual(parent['priority']['id'],'1')
if __name__=='__main__':unittest.main()
