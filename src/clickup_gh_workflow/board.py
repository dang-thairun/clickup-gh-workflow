"""Read-only delivery board: ClickUp cards, their dependencies and GitHub PRs in one HTML file.

Sources
- Specs (`Implementation/*-spec.md`) give spec IDs and the Dependency graph, parsed by the engine.
- One `tasks` read gives every card's status, parent and URL (or the cache with --offline).
- Card comments come from the newest `read` receipt or the board cache; --comments reads every
  spec card again (one read per card, slow through Actions).
- `gh` gives PRs, reviews, unresolved review threads and checks.

Times are shown in the machine's local timezone. Nothing is written to ClickUp or GitHub.
"""
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE = '.team-workflow/board-cache.json'
ROLES = ('backlog', 'ready', 'in_progress', 'in_review', 'rework')
# Status-sync bots often post with a person's token; such comments are not that person talking.
DEFAULT_IGNORE = ('GitHub workflow sync:',)


def _ms(dt):
    return int(dt.timestamp() * 1000)


def _local(ms=None, iso=None, fmt='%Y-%m-%d'):
    dt = datetime.fromisoformat(iso.replace('Z', '+00:00')) if iso else datetime.fromtimestamp(int(ms) / 1000)
    return dt.astimezone().strftime(fmt)


def _text(c):
    t = c.get('comment_text') or ''
    if not t and isinstance(c.get('comment'), list):
        t = ''.join(p.get('text', '') for p in c['comment'])
    return t.strip()


def _who(user, person):
    """person is a set of strings (id, username, email); match any."""
    user = user or {}
    return bool(person) and bool({str(user.get(k)) for k in ('id', 'username', 'email') if user.get(k)} & person)


# ---------------------------------------------------------------- model (pure, tested)

def model(specs, deps, tasks, comments, prs, cfg, me, lead=None, now_ms=None, ignore=DEFAULT_IGNORE):
    """specs: spec IDs in plan order; deps: {spec: [spec]}; tasks: `tasks` rows (include closed);
    comments: {custom_id: [ClickUp comment]}; prs: rows from prs(); me/lead: sets of id/username/email."""
    status_role = {str(v).lower(): k for k, v in (cfg.get('statuses') or {}).items() if k in ROLES}
    known = set(specs)
    rows = {t['custom_id']: t for t in tasks if t.get('custom_id')}
    spec_rows = {t['spec_id']: t for t in tasks if t.get('spec_id') in known}
    parents = {t['parent'] for t in spec_rows.values() if t.get('parent')}
    groups = {t['id']: t for t in tasks if t['id'] in parents}
    merged = {c for p in prs if p['state'] == 'MERGED' for c in p['cards']}
    pr_by_card = {}
    for p in prs:
        for c in p['cards'] if p['state'] != 'CLOSED' else []:
            pr_by_card.setdefault(c, []).append(p['n'])

    cards = []
    for spec in specs:
        t = spec_rows.get(spec)
        if not t:
            continue  # spec not created in ClickUp yet
        cid = t.get('custom_id') or t['id']
        status = (t.get('status') or '').lower()
        role = status_role.get(status, 'later')
        thread = sorted([c for c in comments.get(cid, []) if not _text(c).startswith(tuple(ignore))],
                        key=lambda c: int(c.get('date') or 0))
        mine = [int(c['date']) for c in thread if _who(c.get('user'), me)]
        theirs = [int(c['date']) for c in thread if (_who(c.get('user'), lead) if lead else not _who(c.get('user'), me))]
        last_them = max(theirs or [0])
        asked = [d for d in mine if d > last_them]
        last = thread[-1] if thread else None
        g = groups.get(t.get('parent'))
        cards.append({
            'id': cid, 'spec': spec, 'title': re.sub(r'^\[[^\]]+\]\s*', '', t.get('name') or ''), 'url': t.get('url'),
            'status': t.get('status') or '?', 'role': role,
            # past the gate for cards that wait on it: its PR merged, or ClickUp marks the status done/closed
            'passed': cid in merged or t.get('status_type') in ('done', 'closed'),
            'group': g['name'] if g else 'Other', 'group_link': g and {'id': g.get('custom_id') or g['id'], 'url': g.get('url')},
            'deps': deps.get(spec, []), 'prs': pr_by_card.get(cid, []),
            'lead_new': last_them > max(mine or [0]), 'waiting_since': min(asked) if asked else None,
            'last_comment': last and {'by': (last.get('user') or {}).get('username', '?'),
                                      'at': _local(last['date'], fmt='%Y-%m-%d %H:%M'), 'text': _text(last)[:160]}})
    by_spec = {c['spec']: c for c in cards}
    for c in cards:
        c['deps_done'] = all(by_spec[d]['passed'] for d in c['deps'] if d in by_spec)
        c['deps'] = [by_spec[d]['id'] for d in c['deps'] if d in by_spec]

    for c in cards:
        if c['role'] == 'later' and not c['passed']:
            c['role'] = 'approved'  # past review in ClickUp but not merged yet, e.g. "ready to merge"
    names = [{'role': r, 'name': cfg['statuses'][r]} for r in (cfg.get('statuses') or {}) if r in ROLES]
    names += [{'role': r, 'name': s} for r, s in sorted({(c['role'], c['status']) for c in cards if c['role'] in ('approved', 'later')})]
    now = datetime.fromtimestamp(now_ms / 1000).astimezone() if now_ms else datetime.now().astimezone()
    off = now.strftime('%z')
    tz = 'GMT' + off[0] + str(int(off[1:3])) + (':' + off[3:] if off[3:] != '00' else '')
    return {'generated': now.strftime('%Y-%m-%d %H:%M ') + tz, 'repo': cfg.get('repository', ''),
            'cards': cards, 'prs': prs, 'statuses': names}


def render(data):
    return (HERE / 'templates/board.html').read_text().replace('/*DATA*/null', json.dumps(data, ensure_ascii=False))


# ---------------------------------------------------------------- IO

def _sh(root, *cmd):
    return subprocess.run(cmd, cwd=root, capture_output=True, text=True, check=True).stdout


def prs(root, repo, id_pattern, me_login):
    owner, name = repo.split('/')
    out = []
    rows = json.loads(_sh(root, 'gh', 'pr', 'list', '-R', repo, '--state', 'all', '--limit', '40', '--json',
                          'number,title,state,isDraft,headRefName,reviewDecision,mergedAt,url,body,statusCheckRollup'))
    for p in rows:
        ids = sorted(set(re.findall(id_pattern, p['body'] + ' ' + p['headRefName'] + ' ' + p['title'])))
        if not ids and p['state'] != 'OPEN':
            continue  # old PRs from before cards; open ones stay visible even without a card
        q = ('{repository(owner:"%s",name:"%s"){pullRequest(number:%d){reviews(last:5){nodes{author{login} state submittedAt body}}'
             ' reviewThreads(first:60){nodes{isResolved comments(first:1){nodes{author{login} path body}}}}}}}') % (owner, name, p['number'])
        g = json.loads(_sh(root, 'gh', 'api', 'graphql', '-f', 'query=' + q))['data']['repository']['pullRequest']
        threads = [{'resolved': t['isResolved'], 'by': t['comments']['nodes'][0]['author']['login'],
                    'path': (t['comments']['nodes'][0]['path'] or '').split('/')[-1],
                    'text': re.sub(r'\s+', ' ', re.sub(r'[*`#]', '', t['comments']['nodes'][0]['body']))[:140]}
                   for t in g['reviewThreads']['nodes'] if t['comments']['nodes']]
        reviews = [{'by': r['author']['login'], 'state': r['state'], 'at': _local(iso=r['submittedAt']),
                    'text': next((l.strip('# ').strip() for l in r['body'].splitlines() if l.strip()), '')[:140]}
                   for r in g['reviews']['nodes'] if r['author']['login'] != me_login]
        checks = {}
        for c in p['statusCheckRollup']:
            checks[c.get('name') or c.get('context')] = c.get('conclusion') or c.get('state')  # last run wins
        out.append({'n': p['number'], 'title': re.sub(r'^\[[A-Z0-9_]+-\d+\]\s*|^\w+\([A-Z0-9_]+-\d+\):\s*', '', p['title']),
                    'state': 'DRAFT' if p['isDraft'] and p['state'] == 'OPEN' else p['state'],
                    'review': p['reviewDecision'], 'merged': _local(iso=p['mergedAt']) if p['mergedAt'] else '',
                    'url': p['url'], 'branch': p['headRefName'], 'cards': ids, 'checks': checks,
                    'threads': threads, 'reviews': reviews})
    return out


def receipt_comments(root):
    """Newest comments per card from `read` receipts: {custom_id: (fetched_ms, comments)}."""
    found = {}
    for f in sorted((root / '.team-workflow/receipts').glob('*/**/result.json')):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        r = d.get('result') or {}
        stamp = re.match(r'(\d{8}T\d{6}Z)', f.relative_to(root / '.team-workflow/receipts').parts[0])
        if d.get('operation') == 'read' and r.get('custom_id') and stamp:
            at = _ms(datetime.strptime(stamp[1], '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc))
            found[r['custom_id']] = (at, r.get('comments') or [])
    return found


def build(root, cfg, engine, rpc, out, me=None, lead=None, comments=False, offline=False):
    root = Path(root)
    cache_path = root / CACHE
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    board_cfg = cfg.get('board') or {}
    gh_login = _sh(root, 'gh', 'api', 'user', '--jq', '.login').strip()
    saved = root / '.team-workflow/me.json'
    me_set = {str(v) for v in (json.loads(saved.read_text()) if saved.exists() else {}).values() if v} if not me else {me}
    me_set |= {gh_login}
    lead = lead or board_cfg.get('lead')
    lead_set = {str(lead)} if lead else None

    cards = engine.parse_cards(root)
    specs = [c['spec_id'] for c in cards]
    deps = {c['spec_id']: re.findall(engine.TASK_ID, (re.search(r'\*\*Dependency:\*\* (.+)', c['description']) or [None, ''])[1])
            for c in cards}

    if offline:
        if 'tasks' not in cache:
            raise ValueError('No board cache yet; run board once without --offline')
        tasks = cache['tasks']
    else:
        tasks = rpc('tasks', None, False, {'include_closed': True})['tasks']
        cache['tasks'], cache['tasks_at'] = tasks, _ms(datetime.now())

    stored = cache.setdefault('comments', {})
    for cid, (at, thread) in receipt_comments(root).items():
        if at > stored.get(cid, {}).get('at', 0):
            stored[cid] = {'at': at, 'comments': thread}
    if comments and not offline:
        for t in tasks:
            if t.get('spec_id') in set(specs) and t.get('custom_id'):
                stored[t['custom_id']] = {'at': _ms(datetime.now()), 'comments': rpc('read', t['custom_id']).get('comments') or []}

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False))
    pr_rows = prs(root, cfg['repository'], engine.custom_id_pattern(cfg), gh_login)
    data = model(specs, deps, tasks, {k: v['comments'] for k, v in stored.items()}, pr_rows, cfg,
                 me_set, lead_set, ignore=tuple(board_cfg.get('ignore_comment_prefixes', DEFAULT_IGNORE)))
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data))
    return {'board': str(out), 'cards': len(data['cards']), 'prs': len(pr_rows), 'generated': data['generated'],
            'specs_without_card': [s for s in specs if s not in {c['spec'] for c in data['cards']}],
            'comments_cached': len(stored), 'tasks_from': 'cache' if offline else 'live'}
