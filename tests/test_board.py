import importlib.util
import json
from pathlib import Path
import re
import unittest

PACKAGE = Path(__file__).resolve().parents[1] / 'src/clickup_gh_workflow'
spec = importlib.util.spec_from_file_location('board_under_test', PACKAGE / 'board.py')
board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(board)

CFG = {'repository': 'example-org/example-app',
       'statuses': {'backlog': 'backlog', 'ready': 'ready', 'in_progress': 'in progress', 'in_review': 'in review', 'rework': 'rework'}}
ME, LEAD = {'77', 'dang'}, {'88'}
DAY = 86400000


def row(ident, spec, status, parent='g1', kind='custom'):
    return {'id': ident, 'custom_id': 'TASK-' + ident, 'name': f'[{spec}] Title {spec}', 'spec_id': spec,
            'status': status, 'status_type': kind, 'parent': parent, 'url': 'https://app.clickup.com/t/' + ident}


def comment(user, day, text='note'):
    return {'user': {'id': int(user), 'username': {'77': 'dang', '88': 'lead'}[user]}, 'date': str(day * DAY), 'comment_text': text}


def pr(n, state, cards):
    return {'n': n, 'state': state, 'cards': cards, 'title': 't', 'review': None, 'merged': '', 'url': '', 'branch': '',
            'checks': {}, 'threads': [], 'reviews': []}


TASKS = [
    {'id': 'g1', 'custom_id': 'TASK-1', 'name': 'Group one', 'status': 'in progress', 'url': 'https://app.clickup.com/t/g1'},
    row('10', 'APP-010', 'testing'),
    row('20', 'APP-020', 'ready to merge'),
    row('30', 'APP-030', 'backlog'),
    row('40', 'APP-040', 'in review'),
    row('50', 'APP-050', 'complete', kind='closed'),
    {'id': 's1', 'custom_id': 'TASK-99', 'name': 'step subtask', 'status': 'backlog', 'parent': '10'},
]
DEPS = {'APP-010': [], 'APP-020': ['APP-010'], 'APP-030': ['APP-020'], 'APP-040': ['APP-050'], 'APP-050': []}
SPECS = ['APP-010', 'APP-020', 'APP-030', 'APP-040', 'APP-050']


def build(comments=None, prs=None, lead=LEAD):
    data = board.model(SPECS, DEPS, TASKS, comments or {}, prs or [pr(1, 'MERGED', ['TASK-10'])], CFG, ME, lead, now_ms=30 * DAY)
    return {c['spec']: c for c in data['cards']}, data


class BoardModelTests(unittest.TestCase):
    def test_only_spec_cards_become_cards_and_parents_become_lanes(self):
        cards, _ = build()
        self.assertEqual(sorted(cards), SPECS)
        self.assertEqual(cards['APP-010']['group'], 'Group one')
        self.assertEqual(cards['APP-010']['group_link'], {'id': 'TASK-1', 'url': 'https://app.clickup.com/t/g1'})

    def test_roles_follow_configured_statuses_and_unknown_is_later(self):
        cards, data = build()
        self.assertEqual(cards['APP-030']['role'], 'backlog')
        self.assertEqual(cards['APP-040']['role'], 'in_review')
        self.assertEqual(cards['APP-020']['role'], 'approved')  # past review, not merged
        self.assertEqual(cards['APP-010']['role'], 'later')     # merged
        self.assertIn({'role': 'approved', 'name': 'ready to merge'}, data['statuses'])
        self.assertIn({'role': 'later', 'name': 'testing'}, data['statuses'])

    def test_a_card_is_passed_only_when_merged_or_closed(self):
        cards, _ = build()
        self.assertTrue(cards['APP-010']['passed'])   # PR merged
        self.assertFalse(cards['APP-020']['passed'])  # "ready to merge" is not merged
        self.assertTrue(cards['APP-050']['passed'])   # ClickUp status type closed
        self.assertTrue(cards['APP-020']['deps_done'])
        self.assertFalse(cards['APP-030']['deps_done'])  # waits on the unmerged APP-020
        self.assertTrue(cards['APP-040']['deps_done'])
        self.assertEqual(cards['APP-030']['deps'], ['TASK-20'])

    def test_closed_prs_are_not_linked_to_cards(self):
        cards, _ = build(prs=[pr(1, 'MERGED', ['TASK-10']), pr(2, 'CLOSED', ['TASK-40']), pr(3, 'OPEN', ['TASK-40'])])
        self.assertEqual(cards['APP-040']['prs'], [3])

    def test_lead_answer_after_my_last_comment_is_new(self):
        cards, _ = build({'TASK-40': [comment('77', 1), comment('88', 2)]})
        self.assertTrue(cards['APP-040']['lead_new'])
        self.assertIsNone(cards['APP-040']['waiting_since'])

    def test_waiting_starts_at_my_first_comment_after_the_lead_spoke(self):
        cards, _ = build({'TASK-40': [comment('88', 1), comment('77', 3), comment('77', 5)]})
        self.assertFalse(cards['APP-040']['lead_new'])
        self.assertEqual(cards['APP-040']['waiting_since'], 3 * DAY)

    def test_bot_comments_posted_with_the_lead_token_are_ignored(self):
        cards, _ = build({'TASK-40': [comment('77', 1), comment('88', 2, 'GitHub workflow sync: PR #3 was merged')]})
        self.assertFalse(cards['APP-040']['lead_new'])
        self.assertEqual(cards['APP-040']['last_comment']['by'], 'dang')

    def test_without_a_lead_anyone_but_me_counts(self):
        cards, _ = build({'TASK-40': [comment('77', 1), comment('88', 2)]}, lead=None)
        self.assertTrue(cards['APP-040']['lead_new'])

    def test_render_embeds_data_once(self):
        _, data = build()
        html = board.render(data)
        self.assertNotIn('/*DATA*/null', html)
        embedded = json.loads(re.search(r'const D = (\{.*?\});\n', html, re.S).group(1))
        self.assertEqual(len(embedded['cards']), 5)


if __name__ == '__main__':
    unittest.main()
