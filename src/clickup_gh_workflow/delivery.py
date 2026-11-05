"""Local Git, test evidence and GitHub PR lifecycle. Never merges PRs."""
import fnmatch
import hashlib
import json
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path


def now(): return datetime.now(timezone.utc).isoformat()


def git(root, *args):
    result = subprocess.run(['git', *args], cwd=root, text=True, capture_output=True)
    if result.returncode:
        raise ValueError('Git operation failed: ' + args[0])
    return result.stdout.strip()


def dirty(root):
    lines = git(root, 'status', '--porcelain', '--untracked-files=all').splitlines()
    return [line for line in lines if not line[3:].startswith('.team-workflow/')]


def state_path(root, ident):
    if not re.fullmatch(r'[A-Z][A-Z0-9_]*-\d+', ident):
        raise ValueError('Delivery commands require a verified custom ClickUp ID')
    return root / '.team-workflow/tasks' / (ident + '.json')


def load(root, ident):
    path = state_path(root, ident)
    if not path.exists():
        raise ValueError('No delivery state; run start after the card is READY')
    return json.loads(path.read_text())


def save(root, ident, state):
    path = state_path(root, ident)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n')


def local_card(r, root, ident):
    cards = [c for c in r['engine']().parse_cards(root) if c['spec_id'] == ident]
    if len(cards) != 1:
        raise ValueError('Missing unique local spec')
    return cards[0]


def owned(card):
    match = re.search(r'\*\*Owned Paths:\*\* (.+)', card['description'])
    paths = re.findall(r'`([^`]+)`', match[1]) if match else []
    if not paths or any(p.startswith('/') or '..' in Path(p).parts for p in paths):
        raise ValueError('Owned Paths are missing or not relative to repository')
    return paths


def changed(root, base):
    paths = set(git(root, 'diff', '--name-only', base, '--').splitlines())
    paths.update(git(root, 'ls-files', '--others', '--exclude-standard').splitlines())
    return sorted(p for p in paths if p and not p.startswith('.team-workflow/'))


def fingerprint(root, base):
    digest = hashlib.sha256()
    for name in changed(root, base):
        path = root / name
        digest.update(name.encode() + b'\0')
        if path.is_symlink():
            digest.update(('symlink:' + str(path.readlink())).encode())
        else:
            digest.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b'deleted')
    return digest.hexdigest()


def scope(root, state, card):
    allowed = owned(card)
    paths = changed(root, state['base_sha'])
    bad = [name for name in paths if name == '.env' or not any(fnmatch.fnmatchcase(name, p) or (p.endswith('/') and name.startswith(p)) for p in allowed)]
    if bad:
        raise ValueError('Paths outside the card ownership: ' + ', '.join(bad))
    return paths


def pr_fields():
    return 'number,url,title,body,isDraft,state,createdAt,mergedAt,baseRefName,headRefName,headRefOid,headRepositoryOwner,headRepository,statusCheckRollup'


def find_pr(r, cfg, ident, required=True):
    result = json.loads(r['command'](['gh', 'pr', 'list', '--repo', cfg['repository'], '--state', 'all', '--limit', '100',
                                     '--search', ident + ' in:body', '--json', 'number,url,body,baseRefName,headRefName,headRepositoryOwner']))
    matches = []
    for pr in result:
        lines = (pr.get('body') or '').splitlines()
        if lines and lines[0] == 'ClickUp-Task: ' + ident and pr['baseRefName'] == cfg['base_branch']:
            if (pr.get('headRepositoryOwner') or {}).get('login', '').casefold() == cfg['fork'].split('/')[0].casefold():
                matches.append(pr)
    if len(matches) > 1:
        raise ValueError('Multiple PRs for one card; reconcile before proceeding')
    if not matches:
        if required: raise ValueError('No PR found for ' + ident)
        return None
    return json.loads(r['command'](['gh', 'pr', 'view', str(matches[0]['number']), '--repo', cfg['repository'], '--json', pr_fields()]))


def start(r, rpc, cfg, root, ident, args):
    remote = rpc('resolve-start', ident, False, {})
    ident = remote['custom_id']
    for dependency in remote['dependencies']:
        pr = find_pr(r, cfg, dependency['custom_id'])
        if pr['state'] != 'MERGED':
            raise ValueError('Dependency PR has not merged: ' + dependency['custom_id'])
    existing = find_pr(r, cfg, ident, required=False)
    if existing:
        raise ValueError('This card already has a PR; use pr read/check instead of starting another')
    title = re.sub(r'^\[[^]]+\]\s*', '', remote['name'])
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:55] or 'task'
    branch = args.type + '/' + ident + '-' + slug
    preview = {'apply': args.apply, 'clickup_id': ident, 'branch': branch, 'base': cfg['base_branch'],
               'fork': cfg['fork'], 'dirty_paths': dirty(root), 'next': 'Write RED tests, capture check evidence and open a Draft PR. Stop for Dev verification.'}
    if not args.apply:
        return preview
    if dirty(root):
        raise ValueError('Working tree has changes; commit/save them before start. Nothing was stashed or reset')
    if state_path(root, ident).exists():
        raise ValueError('Delivery state already exists; do not create a second branch')
    exists = subprocess.run(['git', 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch], cwd=root)
    if exists.returncode == 0:
        raise ValueError('Branch already exists; inspect it instead of replacing it')
    r['command'](['gh', 'repo', 'sync', cfg['fork'], '--source', cfg['repository'], '--branch', cfg['base_branch']])
    git(root, 'fetch', 'https://github.com/' + cfg['fork'] + '.git', cfg['base_branch'])
    base = git(root, 'rev-parse', 'FETCH_HEAD')
    # Approved planning/config must be available on the implementation base.
    planning = [root/'.clickup-workflow.toml', *(root/'Implementation').glob('*-spec.md')]
    for path in planning:
        rel = str(path.relative_to(root))
        try:
            stored = git(root, 'show', base + ':' + rel)
        except ValueError:
            raise ValueError('Merge the approved planning/config into the base branch before start: ' + rel) from None
        if stored != path.read_text().strip():
            raise ValueError('Base branch planning differs from the local approved plan: ' + rel)
    git(root, 'switch', '-c', branch, base)
    state = dict(preview, spec_id=remote['spec_id'], spec=remote['spec'], base_sha=base, checks=[], started_at=now())
    save(root, ident, state)
    return state


def check(r, cfg, root, ident, args):
    state = load(root, ident)
    if git(root, 'branch', '--show-current') != state['branch']:
        raise ValueError('Current branch is not the task branch')
    if args.phase == 'green' and not state.get('red_approval'):
        raise ValueError('RED must be explicitly approved before GREEN')
    if args.phase == 'red' and not args.reason:
        raise ValueError('RED requires --reason describing the business failure to inspect')
    command = list(args.command)
    if command and command[0] == '--': command.pop(0)
    if not command:
        raise ValueError('Provide the actual test command after --; no shell interpolation is performed')
    card = local_card(r, root, state['spec_id'])
    scope(root, state, card)
    before = fingerprint(root, state['base_sha'])
    directory = root / '.team-workflow/checks' / ident
    directory.mkdir(parents=True, exist_ok=True)
    label = args.label
    if not re.fullmatch(r'[a-zA-Z0-9_-]+', label):
        raise ValueError('Invalid check label')
    key = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    log = directory / (key + '.log')
    import os
    environment = dict(os.environ)
    environment.pop('CLICKUP_API_TOKEN', None); environment.pop('GITHUB_TOKEN', None)
    with log.open('w') as output:
        try:
            result = subprocess.run(command, cwd=root, stdout=output, stderr=subprocess.STDOUT, env=environment, timeout=args.timeout)
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = 124
        except OSError as exc:
            output.write(str(exc)); code = 127
    after = fingerprint(root, state['base_sha'])
    valid = before == after and ((code != 0 and code not in (124, 126, 127) and code > 0) if args.phase == 'red' else code == 0)
    evidence = {'phase': args.phase, 'label': label, 'command': command, 'exit_code': code, 'valid': valid,
                'fingerprint': after, 'reason': args.reason, 'created_at': now(), 'log': str(log.relative_to(root))}
    state['checks'].append(evidence)
    if args.phase == 'red': state.pop('red_approval', None)
    save(root, ident, state)
    if not valid:
        raise ValueError('Check does not satisfy phase or changed source files; evidence saved at ' + str(log))
    return dict(evidence, note='RED failure reason still requires human verification; logs stay local unless explicitly published')


def approve_red(root, ident, args):
    state = load(root, ident)
    candidates = [c for c in state['checks'] if c['phase'] == 'red' and c['valid']]
    if not candidates or candidates[-1]['fingerprint'] != fingerprint(root, state['base_sha']):
        raise ValueError('No current valid RED evidence')
    result = {'apply': args.apply, 'clickup_id': ident, 'red_evidence': candidates[-1], 'reason': args.reason}
    if not args.reason:
        raise ValueError('Record the human verification reason using --reason')
    if args.apply:
        state['red_approval'] = {'at': now(), 'reason': args.reason, 'fingerprint': candidates[-1]['fingerprint']}
        save(root, ident, state)
    return result


def green_gate(r, cfg, root, ident, state):
    card = local_card(r, root, state['spec_id'])
    scope(root, state, card)
    if not state.get('red_approval'):
        raise ValueError('No explicit RED approval recorded')
    current = fingerprint(root, state['base_sha'])
    latest = {}
    for entry in state['checks']:
        if entry['phase'] == 'green': latest[entry['label']] = entry
    required = cfg.get('delivery', {}).get('required_checks', ['test'])
    if not required:
        raise ValueError('At least one required GREEN check must be configured')
    for label in required:
        if label not in latest or not latest[label]['valid'] or latest[label]['fingerprint'] != current:
            raise ValueError('Missing/pending/stale GREEN evidence for ' + label)
    return [latest[label] for label in required]


def commit_gate(root, ident, state):
    if dirty(root):
        raise ValueError('Commit task changes before opening/readying the PR')
    messages = git(root, 'log', state['base_sha'] + '..HEAD', '--format=%B%x00').split('\x00')
    messages = [message.strip() for message in messages if message.strip()]
    if not messages:
        raise ValueError('No task commits to publish')
    if any(message.splitlines()[-1] != 'ClickUp-Task: ' + ident for message in messages):
        raise ValueError('Each task commit must end with ClickUp-Task: ' + ident)


def ci_gate(cfg, pr):
    checks = pr.get('statusCheckRollup') or []
    if not checks and not cfg.get('delivery', {}).get('allow_no_ci', False):
        raise ValueError('No CI checks visible; wait for CI or explicitly configure delivery.allow_no_ci')
    for item in checks:
        state = item.get('state')
        if state:
            if state.upper() != 'SUCCESS': raise ValueError('CI status is not successful')
        elif item.get('status') != 'COMPLETED' or item.get('conclusion') not in ('SUCCESS', 'NEUTRAL', 'SKIPPED'):
            raise ValueError('CI checks are failing or pending')


def metadata(rpc, pr, ident, apply):
    return rpc('pr-metadata', ident, apply, {'url': pr['url'], 'base': pr['baseRefName'],
               'created_at': pr['createdAt'], 'merged_at': pr.get('mergedAt'),
               'draft': pr['isDraft'], 'state': pr['state']})


def pr_command(r, rpc, cfg, root, ident, args):
    if not re.fullmatch(r'[A-Z][A-Z0-9_]*-\d+', ident): raise ValueError('PR commands require a verified custom ClickUp ID')
    action = args.action
    if action in ('read', 'checks', 'sync'):
        pr = find_pr(r, cfg, ident)
        if action == 'checks': return {'url': pr['url'], 'checks': pr.get('statusCheckRollup') or []}
        if action == 'sync': return {'pr': pr, 'clickup': metadata(rpc, pr, ident, args.apply)}
        return pr
    state = load(root, ident)
    if git(root, 'branch', '--show-current') != state['branch']:
        raise ValueError('Current branch is not the task branch')
    card = local_card(r, root, state['spec_id'])
    scope(root, state, card)
    pr = find_pr(r, cfg, ident, required=False)
    if action == 'open':
        if pr:
            return {'pr': pr, 'note': 'Existing PR returned; no duplicate was created. Use pr sync for ClickUp metadata.'}
        reds = [c for c in state['checks'] if c['phase'] == 'red' and c['valid']]
        if not reds or reds[-1]['fingerprint'] != fingerprint(root, state['base_sha']):
            raise ValueError('Capture current RED evidence before opening Draft PR')
        commit_gate(root, ident, state)
        title = '[' + ident + '] ' + re.sub(r'^\[[^]]+\]\s*', '', card['name'])
        context = re.search(r'\*\*Context & Scope:\*\* (.+)', card['description'])
        body = '\n'.join(['ClickUp-Task: ' + ident, '', '## Task', state['spec_id'], '', '## Goal',
                          context[1] if context else card['name'], '', '## Architecture References', '- ' + card['spec'],
                          '', '## TDD', '### RED', '- Command: ' + json.dumps(reds[-1]['command']),
                          '- Exit code: ' + str(reds[-1]['exit_code']), '- Reason to verify: ' + reds[-1]['reason'],
                          '### GREEN', 'Pending; stop for Dev RED verification.', '### REFACTOR', 'Pending.',
                          '', '## Changes', *['- `' + p + '`' for p in changed(root, state['base_sha'])],
                          '', '## Evidence', 'Local RED log: ' + reds[-1]['log'] + ' (not uploaded automatically).'])
        preview = {'apply': args.apply, 'head': cfg['fork'].split('/')[0] + ':' + state['branch'],
                   'base': cfg['base_branch'], 'title': title, 'body': body, 'draft': True}
        if not args.apply: return preview
        git(root, 'push', 'https://github.com/' + cfg['fork'] + '.git', 'HEAD:refs/heads/' + state['branch'])
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as file:
            file.write(body); filename = file.name
        r['command'](['gh', 'pr', 'create', '--repo', cfg['repository'], '--base', cfg['base_branch'],
                      '--head', preview['head'], '--title', title, '--body-file', filename, '--draft'])
        pr = find_pr(r, cfg, ident)
        state['pr_url'] = pr['url']; save(root, ident, state)
        return {'pr': pr, 'clickup': metadata(rpc, pr, ident, True), 'next': 'STOP: Dev must verify RED before GREEN'}
    if not pr or pr['state'] != 'OPEN': raise ValueError('An open PR is required')
    if action == 'ready':
        evidence = green_gate(r, cfg, root, ident, state)
        commit_gate(root, ident, state)
        if pr['headRefOid'] != git(root, 'rev-parse', 'HEAD'):
            raise ValueError('Push the tested commits and wait for their CI before pr ready')
        ci_gate(cfg, pr)
        if args.apply:
            # Replace only CLI-generated pending GREEN/REFACTOR text, preserve review notes.
            body = pr['body']
            old = '### GREEN\nPending; stop for Dev RED verification.\n### REFACTOR\nPending.'
            evidence_lines = '\n'.join('- ' + c['label'] + ': exit 0; ' + json.dumps(c['command']) for c in evidence)
            new = '### GREEN\n' + evidence_lines + '\n### REFACTOR\nOwned paths validated; review the final diff for refactoring details.'
            if old in body:
                with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as file:
                    file.write(body.replace(old, new)); filename = file.name
                r['command'](['gh', 'pr', 'edit', str(pr['number']), '--repo', cfg['repository'], '--body-file', filename])
            r['command'](['gh', 'pr', 'ready', str(pr['number']), '--repo', cfg['repository']])
    elif action == 'draft' and args.apply:
        r['command'](['gh', 'pr', 'ready', str(pr['number']), '--repo', cfg['repository'], '--undo'])
    if args.apply:
        pr = find_pr(r, cfg, ident)
        state['pr_url'] = pr['url']; save(root, ident, state)
        return {'pr': pr, 'clickup': metadata(rpc, pr, ident, True)}
    return {'apply': False, 'action': action, 'pr': pr['url']}
