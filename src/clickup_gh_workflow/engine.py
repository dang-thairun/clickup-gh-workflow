#!/usr/bin/env python3
"""Project-scoped ClickUp delivery CLI. Python 3.11+, standard library only."""
import sys
import os
import shutil
if sys.version_info < (3, 11):
    interpreter = shutil.which('python3.11')
    if interpreter:
        os.execv(interpreter, [interpreter, *sys.argv])
    raise SystemExit('Python 3.11+ is required (TOML support).')
import argparse
import json
import re
import tempfile
import tomllib
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote

TASK_ID = r'[A-Z][A-Z0-9]*-\d+'
CUSTOM_ID = r'DEV-\d+'


def custom_id_pattern(cfg):
    prefix = cfg.get('custom_id_prefix', 'DEV')
    if not re.fullmatch(r'[A-Z][A-Z0-9_]*', prefix):
        raise ValueError('custom_id_prefix must contain uppercase letters, digits or underscores')
    return re.escape(prefix) + r'-\d+'



def request(method, path, token, body=None, github=False):
    host = 'https://api.github.com' if github else 'https://api.clickup.com/api/v2'
    headers = {'Authorization': ('Bearer ' if github else '') + token,
               'Content-Type': 'application/json', 'User-Agent': 'clickup-gh-workflow/1.0.4'}
    req = Request(host + path, data=None if body is None else json.dumps(body).encode(),
                  headers=headers, method=method)
    try:
        with urlopen(req, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        # Do not print bodies/headers: they can contain private data. Never retry a write blindly.
        raise ValueError(f'{method} failed: HTTP {exc.code}; inspect remote state before retrying writes') from None
    except (URLError, TimeoutError) as exc:
        raise ValueError(f'{method} network failure; write outcome may be unknown, inspect before retrying') from None


def token(name='CLICKUP_API_TOKEN'):
    value = os.environ.get(name)
    if not value:
        raise ValueError(f'Missing {name}; provide it in the environment, never in a spec or CLI argument')
    return value


def parse_cards(root):
    cards = []
    for path in sorted((root / 'Implementation').glob('*-spec.md')):
        text = path.read_text()
        for match in re.finditer(rf'^## \[({TASK_ID})\] ([^\n]+)\n(.*?)(?=^## |\Z)', text, re.M | re.S):
            ident, title, content = match.groups()
            estimate = re.search(r'Type / Points / Estimate:\*\* \w+ / (\d+) / (\d+)h', content)
            if not estimate or int(estimate[1]) not in (1,2,3,5,8):
                raise ValueError(f'{ident}: missing/invalid estimate')
            subtasks = content.split('- **Subtasks:**', 1)
            steps = re.findall(r'^  - \[[ xX]\] (.+)$', subtasks[1], re.M) if len(subtasks) == 2 else []
            allocation = re.search(r'Subtask Estimates \(minutes\):\*\* ([0-9, ]+)', content)
            minutes = [int(v.strip()) for v in allocation[1].split(',')] if allocation else []
            if steps and (len(minutes) != len(steps) or any(v <= 0 for v in minutes) or sum(minutes) != int(estimate[2]) * 60):
                raise ValueError(f'{ident}: subtask estimates must match the steps and total')
            cards.append({'spec_id': ident, 'name': f'[{ident}] {title}',
                          'spec': str(path.relative_to(root)), 'points': int(estimate[1]),
                          'time_estimate': int(estimate[2]) * 3600000,
                          'description': f'Spec: {path.relative_to(root)}\n\n{content.strip()}',
                          'subtasks': steps, 'subtask_estimates_ms': [v * 60000 for v in minutes]})
    ids = [c['spec_id'] for c in cards]
    if not ids or len(ids) != len(set(ids)):
        raise ValueError('No cards found or duplicate spec IDs')
    known = set(ids)
    graph = {}
    for c in cards:
        deps = re.search(r'\*\*Dependency:\*\* (.+)', c['description'])
        graph[c['spec_id']] = re.findall(TASK_ID, deps[1]) if deps else []
    active, done = set(), set()
    def visit(ident):
        if ident not in known or ident in active:
            raise ValueError(f'Missing or circular dependency: {ident}')
        if ident in done:
            return
        active.add(ident)
        for dep in graph[ident]:
            visit(dep)
        active.remove(ident)
        done.add(ident)
    for ident in ids:
        visit(ident)
    return cards


def api_list(cfg):
    return request('GET', '/list/' + cfg['list_id'], token())


def status_name(task):
    return task['status']['status'].casefold()


def doctor(cfg, required_statuses=None):
    listing = api_list(cfg)
    actual = {s['status'].casefold() for s in listing['statuses']}
    expected = set(cfg['statuses'].values()) if required_statuses is None else set(required_statuses)
    missing = expected - actual
    if missing:
        raise ValueError('List is accessible but configured statuses do not exist: ' + ', '.join(sorted(missing)))
    return {'list_id': listing['id'], 'list_name': listing['name'], 'statuses': sorted(actual)}


def all_tasks(cfg):
    result = []
    # Include archived/closed/subtasks and tasks in multiple lists to avoid duplicate creation.
    for archived in ('false', 'true'):
        page = 0
        while True:
            query = urlencode({'page': page, 'archived': archived, 'include_closed': 'true',
                               'subtasks': 'true', 'include_timl': 'true'})
            response = request('GET', f'/list/{cfg["list_id"]}/task?{query}', token())
            batch = response['tasks']
            result.extend(batch)
            if not batch or response.get('last_page', False):
                break
            page += 1
    return list({t['id']: t for t in result}.values())


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as out:
        json.dump(data, out, ensure_ascii=False, indent=2)
        temp = out.name
    os.replace(temp, path)


def create_cards(cfg, cards, root, apply=False):
    # Online preview is read-only; the offline `plan` needs no credentials.
    priority_name = cfg.get('defaults', {}).get('priority', 'normal')
    priorities = {'urgent':1, 'high':2, 'normal':3, 'low':4, 'none':None}
    if priority_name not in priorities:
        raise ValueError('Unknown default priority; expected urgent/high/normal/low/none')
    default_priority = priorities[priority_name]
    doctor(cfg, {cfg['statuses']['backlog']})
    tasks = all_tasks(cfg)
    state_path = root / 'Implementation/clickup-bindings.json'
    bindings = json.loads(state_path.read_text()) if state_path.exists() else {}
    output = []
    for card in cards:
        matches = [t for t in tasks if t['name'].startswith(f'[{card["spec_id"]}]')]
        if len(matches) > 1:
            raise ValueError(f'Duplicate remote spec ID {card["spec_id"]}; resolve manually')
        task = matches[0] if matches else None
        if not task and apply:
            task = request('POST', f'/list/{cfg["list_id"]}/task', token(), {
                'name': card['name'], 'markdown_content': card['description'],
                'status': cfg['statuses']['backlog'], 'points': card['points'], 'priority': default_priority,
                'time_estimate': 0 if card['subtasks'] else card['time_estimate'], 'notify_all': False})
            tasks.append(task)
        if task:
            if str(task['list']['id']) != cfg['list_id']:
                raise ValueError('Existing task has another home List; resolve before binding')
            binding = {'id': task['id'], 'custom_id': task.get('custom_id'),
                       'url': task.get('url'), 'spec': card['spec']}
            bindings[card['spec_id']] = binding
            if apply:
                # Persist after each remote success, including a parent before any subtasks.
                write_json(state_path, bindings)
            for index, step in enumerate(card['subtasks']):
                children = [t for t in tasks if t.get('parent') == task['id'] and t['name'] == step]
                if len(children) > 1:
                    raise ValueError('Duplicate subtask; inspect remote state')
                if not children and apply:
                    child = request('POST', f'/list/{cfg["list_id"]}/task', token(),
                                    {'name': step, 'parent': task['id'], 'status': cfg['statuses']['backlog'],
                                     'time_estimate': card['subtask_estimates_ms'][index], 'priority': default_priority, 'notify_all': False})
                    tasks.append(child)
        output.append({'spec_id': card['spec_id'], 'action': 'bind-existing' if matches else 'create',
                       'custom_id': task.get('custom_id') if task else None,
                       'new_task_priority': priority_name})
    return {'apply': apply, 'cards': output,
            'note': 'Existing task content/status is preserved. Type/Epic/Repository fields and ClickUp dependency links need live field mapping; spec carries those details.'}


def set_subtask_estimates(cfg, card, apply=False):
    if not card['subtasks']:
        raise ValueError('This command requires subtasks')
    tasks = all_tasks(cfg)
    parents = [t for t in tasks if t['name'].startswith(f'[{card["spec_id"]}]')]
    if len(parents) != 1 or parents[0].get('archived'):
        raise ValueError('Existing active parent must match uniquely; no tasks are created')
    parent = parents[0]
    children = [t for t in tasks if t.get('parent') == parent['id']]
    if len(children) != len(card['subtasks']):
        raise ValueError('Remote subtask count differs from spec; no estimates changed')
    updates = []
    for name, estimate in zip(card['subtasks'], card['subtask_estimates_ms']):
        matches = [t for t in children if t['name'] == name and not t.get('archived')]
        if len(matches) != 1:
            raise ValueError('Remote subtask name differs or is duplicated; no estimates changed')
        updates.append({'id': matches[0]['id'], 'name': name, 'time_estimate': estimate,
                        'before': matches[0].get('time_estimate')})
    if sum(u['time_estimate'] for u in updates) != card['time_estimate']:
        raise ValueError('Allocation does not match total')
    def read(ident):
        return request('GET', '/task/' + quote(ident), token())
    if apply:
        # Update children first. Parent is cleared only after all children are verified.
        for update in updates:
            if int(update['before'] or 0) != update['time_estimate']:
                request('PUT', '/task/' + quote(update['id']), token(), {'time_estimate': update['time_estimate']})
            verified = read(update['id'])
            if int(verified.get('time_estimate') or 0) != update['time_estimate']:
                raise ValueError('Subtask estimate did not persist; parent estimate preserved')
        request('PUT', '/task/' + quote(parent['id']), token(), {'time_estimate': 0})
        parent = read(parent['id'])
        if int(parent.get('time_estimate') or 0) != 0:
            raise ValueError('Parent own estimate was not cleared; inspect rollup before retrying')
    return {'apply': apply, 'spec_id': card['spec_id'], 'custom_id': parent.get('custom_id'),
            'parent_id': parent['id'], 'parent_own_estimate_ms': parent.get('time_estimate'),
            'subtasks': updates, 'expected_total_ms': card['time_estimate'],
            'reported_rollup_ms': parent.get('rolled_up_time_estimate'),
            'note': 'Parent display requires Time Estimates Rollup ClickApp; tracked time and statuses are unchanged.'}


def get_task(cfg, ident):
    if not re.fullmatch(custom_id_pattern(cfg), ident):
        raise ValueError('Expected a custom ClickUp ID matching custom_id_prefix, not a spec ID')
    query = urlencode({'custom_task_ids': 'true', 'team_id': cfg['workspace_id']})
    task = request('GET', f'/task/{quote(ident)}?{query}', token())
    if str(task['list']['id']) != cfg['list_id']:
        raise ValueError('Task belongs to another List')
    return task


def inspect_task(cfg, internal_id):
    if not re.fullmatch(r'[A-Za-z0-9]+', internal_id):
        raise ValueError('Expected an internal ClickUp task ID')
    listing = api_list(cfg)
    tasks = all_tasks(cfg)
    matches = [t for t in tasks if t['id'] == internal_id]
    task_error = None
    try:
        task = request('GET', f'/task/{quote(internal_id)}?include_subtasks=true', token())
        source = 'Get Task'
    except ValueError as exc:
        if len(matches) != 1:
            raise ValueError('Cannot read task details and task not found uniquely in List') from exc
        task, source, task_error = matches[0], 'Get Tasks list fallback', str(exc)
    if str(task['list']['id']) != cfg['list_id']:
        raise ValueError('Task belongs to another List')
    space_id = str(listing['space']['id'])
    try:
        space = request('GET', '/space/' + quote(space_id), token())
    except ValueError:
        space = {'name': listing['space'].get('name'), 'features': {}}
    sample_ids = [t['custom_id'] for t in tasks if t.get('custom_id')]
    return {'id': task['id'], 'name': task['name'], 'custom_id': task.get('custom_id'),
            'url': task.get('url'), 'status': status_name(task), 'identity_source': source,
            'task_details_error': task_error,
            'list': {'id': listing['id'], 'name': listing['name']},
            'space': {'id': space_id, 'name': space.get('name'),
                      'custom_task_ids_feature': space.get('features', {}).get('custom_task_ids', 'not exposed by API')},
            'list_custom_ids_sample': sample_ids[:5],
            'subtask_count': len(task['subtasks']) if 'subtasks' in task else sum(t.get('parent') == internal_id for t in tasks)}


def start_task(cfg, ident, root):
    task = get_task(cfg, ident)
    if status_name(task) != cfg['statuses']['ready']:
        raise ValueError('Task is not READY; obtain Lead approval first')
    match = re.match(rf'^\[({TASK_ID})\]', task['name'])
    if not match:
        raise ValueError('Task name is missing a spec ID')
    card = next((c for c in parse_cards(root) if c['spec_id'] == match[1]), None)
    if not card:
        raise ValueError('No matching local spec')
    return {'clickup_id': ident, 'spec_id': card['spec_id'], 'spec': card['spec'],
            'base': cfg['base_branch'], 'fork': cfg['fork'],
            'next': 'Sync fork develop, create task branch; perform RED and stop for Dev verification. Do not auto-merge.'}


def desired_status(cfg, pr, reviews):
    if pr['base']['repo']['full_name'] != cfg['repository'] or pr['base']['ref'] != cfg['base_branch']:
        raise ValueError('Unexpected repository or base branch')
    if pr.get('author_association') not in ('OWNER', 'MEMBER', 'COLLABORATOR'):
        raise ValueError('Only trusted repository authors may trigger status writes')
    if pr.get('merged') or pr.get('state') != 'open':
        return None  # QA deployment must be verified separately; no merge -> testing shortcut.
    if pr.get('draft'):
        return cfg['statuses']['in_progress']
    latest = {}
    for review in sorted(reviews, key=lambda r: r.get('id', 0)):
        if review['state'] in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
            latest[review['user']['login']] = review['state']
    key = 'rework' if 'CHANGES_REQUESTED' in latest.values() else 'in_review'
    return cfg['statuses'][key]


def sync_pr(cfg, event, apply=False):
    if 'workflow_run' in event:
        run = event['workflow_run']
        if run.get('name') != 'ClickUp review signal' or run.get('conclusion') != 'success':
            raise ValueError('Unexpected review signal')
        prs = run.get('pull_requests', [])
        if not prs:
            return {'action': 'skip', 'reason': 'No associated PR; use local sync-pr with current PR number to reconcile'}
        if len(prs) != 1:
            raise ValueError('Ambiguous review signal')
        pr = prs[0]
    else:
        pr = event['pull_request']
    number = int(pr['number'])
    repo = cfg['repository']
    # Resolve the current PR rather than trusting stale event state or its embedded body.
    pr = request('GET', f'/repos/{repo}/pulls/{number}', token('GITHUB_TOKEN'), github=True)
    reviews, page = [], 1
    while True:
        batch = request('GET', f'/repos/{repo}/pulls/{number}/reviews?per_page=100&page={page}', token('GITHUB_TOKEN'), github=True)
        reviews.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    target = desired_status(cfg, pr, reviews)
    if target is None:
        return {'action': 'skip', 'reason': 'closed/merged; QA delivery is separate'}
    lines = (pr.get('body') or '').splitlines()
    match = re.fullmatch(rf'ClickUp-Task: ({custom_id_pattern(cfg)})', lines[0] if lines else '')
    if not match:
        raise ValueError('PR first body line must use ClickUp-Task and the configured custom ID prefix')
    ident = match[1]
    if not re.search(rf'(?<![A-Z0-9]){re.escape(ident)}(?![A-Z0-9])', pr['head']['ref']):
        raise ValueError('Branch must contain the same ClickUp ID')
    task = get_task(cfg, ident)
    current = status_name(task)
    # Protect BACKLOG, BLOCKED, QA, release and terminal states from workflow replays.
    allowed = {cfg['statuses'][k] for k in ('ready', 'in_progress', 'in_review', 'rework')}
    if current not in allowed:
        return {'action': 'skip', 'reason': 'protected state', 'current': current}
    if apply and current != target:
        doctor(cfg)
        request('PUT', '/task/' + quote(task['id']), token(), {'status': target})
    return {'apply': apply, 'clickup_id': ident, 'from': current, 'to': target}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('plan')
    sub.add_parser('doctor')
    create = sub.add_parser('create-cards')
    create.add_argument('--apply', action='store_true')
    create.add_argument('--only', help='Import exactly one spec ID, e.g. AUTH-020')
    start = sub.add_parser('start-task')
    start.add_argument('clickup_id')
    inspect = sub.add_parser('inspect-task')
    inspect.add_argument('internal_id')
    estimates = sub.add_parser('set-subtask-estimates')
    estimates.add_argument('--only', required=True)
    estimates.add_argument('--apply', action='store_true')
    sync = sub.add_parser('sync-pr')
    sync.add_argument('--event-file', required=True, type=Path)
    sync.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    cfg = tomllib.loads((root / '.clickup-workflow.toml').read_text())
    if args.command == 'plan':
        result = {'cards': parse_cards(root)}
    elif args.command == 'doctor':
        result = doctor(cfg)
    elif args.command == 'create-cards':
        cards = parse_cards(root)
        if args.only:
            cards = [card for card in cards if card['spec_id'] == args.only]
            if len(cards) != 1:
                raise ValueError('Requested spec ID is missing or ambiguous; no API calls made')
        if args.apply:
            import fcntl
            lock = root / 'Implementation/.clickup-import.lock'
            with lock.open('w') as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ValueError('Another import is active in this checkout')
                result = create_cards(cfg, cards, root, True)
        else:
            result = create_cards(cfg, cards, root, False)
    elif args.command == 'set-subtask-estimates':
        cards = [c for c in parse_cards(root) if c['spec_id'] == args.only]
        if len(cards) != 1:
            raise ValueError('Requested spec ID missing or ambiguous')
        result = set_subtask_estimates(cfg, cards[0], args.apply)
    elif args.command == 'inspect-task':
        result = inspect_task(cfg, args.internal_id)
    elif args.command == 'start-task':
        result = start_task(cfg, args.clickup_id, root)
    else:
        result = sync_pr(cfg, json.loads(args.event_file.read_text()), args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, KeyError, OSError) as exc:
        print(f'Error: {exc}', file=sys.stderr)
        sys.exit(1)
