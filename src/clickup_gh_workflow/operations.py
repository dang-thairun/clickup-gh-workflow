"""ClickUp operations shared by local CLI and GitHub worker. No provider SDK."""
import difflib
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

BEGIN = '[clickup-gh-workflow:spec-begin]'
END = '[clickup-gh-workflow:spec-end]'
PRIORITIES = {'urgent': 1, 'high': 2, 'normal': 3, 'low': 4, 'none': None}
WRITES = {'create-card', 'set-estimates', 'update', 'estimate', 'status', 'sync', 'time-log', 'time-edit', 'time-delete', 'pr-metadata'}
OPERATIONS = WRITES | {'doctor', 'read-card', 'read', 'diff', 'time-list', 'resolve-start', 'tasks'}


def duration(text):
    matches = list(re.finditer(r'(\d+(?:\.\d+)?)(h|m|s)', text or ''))
    if not matches or ''.join(m.group() for m in matches) != text:
        raise ValueError('Duration must be explicit, e.g. 1h30m, 90m or 45s')
    value = round(sum(float(m[1]) * {'h': 3600000, 'm': 60000, 's': 1000}[m[2]] for m in matches))
    if value <= 0:
        raise ValueError('Duration must be positive')
    return value


def timestamp(text):
    try:
        value = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        raise ValueError('Use an ISO datetime with timezone, e.g. 2026-11-06T17:00:00+07:00') from None
    if value.tzinfo is None:
        raise ValueError('Timezone is required; use +07:00 or Z')
    return int(value.timestamp() * 1000)


def task(e, cfg, ident):
    if re.fullmatch(e.custom_id_pattern(cfg), ident):
        internal = e.get_task(cfg, ident)['id']
    elif re.fullmatch(e.TASK_ID, ident):
        matches = [t for t in e.all_tasks(cfg) if t['name'].startswith(f'[{ident}]')]
        if len(matches) != 1:
            raise ValueError('Spec ID must match one existing parent task')
        internal = matches[0]['id']
    elif re.fullmatch(r'[A-Za-z0-9]+', ident):
        internal = ident
    else:
        raise ValueError('Expected a spec, DEV or internal task ID')
    result = e.request('GET', '/task/' + quote(internal) + '?include_markdown_description=true&include_subtasks=true', e.token())
    if str(result['list']['id']) != cfg['list_id']:
        raise ValueError('Task belongs to another List')
    return result


def card(e, root, ident):
    matches = [c for c in e.parse_cards(root) if c['spec_id'] == ident]
    if len(matches) != 1:
        raise ValueError('Exactly one local spec card is required')
    return matches[0]


def spec_id(remote):
    match = re.match(r'^\[([A-Z][A-Z0-9]*-\d+)\]', remote['name'])
    if not match:
        raise ValueError('Task has no spec ID in its title')
    return match[1]


def description(remote):
    text = remote.get('markdown_description') or remote.get('markdown_content') or remote.get('description') or ''
    # ClickUp escapes literal brackets when returning Markdown after an update.
    return re.sub(r'\\\[(clickup-gh-workflow:[^\]\n]+)\\\]', r'[\1]', text)


def identity(remote):
    return {'id': remote['id'], 'custom_id': remote.get('custom_id'), 'url': remote.get('url')}


def verify_delivery_parent(e, cfg, remote):
    """Summary parents can contain delivery units; step subtasks cannot own PRs."""
    if not remote.get('parent'):
        return
    sid = spec_id(remote)
    groups = [(key, g) for key,g in cfg.get('groups', {}).items() if sid in g['cards']]
    if len(groups) != 1:
        raise ValueError('Only a configured delivery unit can own a branch/PR')
    key, group = groups[0]
    parent = e.request('GET', '/task/' + quote(remote['parent']), e.token())
    if parent.get('parent') or parent.get('name') != group['name'] or '[clickup-gh-workflow:group:' + key + ']' not in description(parent) or str(parent.get('list',{}).get('id')) != cfg['list_id']:
        raise ValueError('Delivery unit is under an unexpected summary parent card')


def field(remote, selector):
    matches = [f for f in remote.get('custom_fields', []) if f['id'] == selector or f['name'].casefold() == selector.casefold()]
    if len(matches) != 1:
        raise ValueError('Custom field must resolve uniquely: ' + selector)
    return matches[0]


def field_value(f, raw):
    kind = f['type']
    if kind == 'drop_down':
        matches = [o for o in f.get('type_config', {}).get('options', []) if o['id'] == raw or o['name'].casefold() == raw.casefold()]
        if len(matches) != 1:
            raise ValueError('Unknown or ambiguous option for ' + f['name'])
        return matches[0]['id']
    if kind in ('text', 'short_text', 'url', 'email', 'phone'):
        return raw
    if kind == 'number':
        return float(raw)
    if kind == 'date':
        return timestamp(raw)
    if kind == 'checkbox' and raw.lower() in ('true', 'false'):
        return raw.lower() == 'true'
    raise ValueError('Unsupported custom field value type: ' + kind)


def update(e, cfg, remote, opts, apply):
    payload = {}
    if 'name' in opts:
        sid = spec_id(remote)
        name = opts['name'].strip()
        if not name:
            raise ValueError('Task title cannot be empty')
        if name.startswith('[') and not name.startswith('[' + sid + '] '):
            raise ValueError('Cannot change the spec ID via rename')
        payload['name'] = name if name.startswith('[') else f'[{sid}] {name}'
    if 'description' in opts:
        payload['markdown_content'] = opts['description']
    if 'priority' in opts:
        if opts['priority'] not in PRIORITIES:
            raise ValueError('Unknown priority')
        payload['priority'] = PRIORITIES[opts['priority']]
    for source, dest in (('due', 'due_date'), ('planned_start', 'start_date')):
        if source in opts:
            payload[dest] = timestamp(opts[source]) if opts[source] else None
            payload[dest + '_time'] = True
    start = payload.get('start_date', remote.get('start_date'))
    due = payload.get('due_date', remote.get('due_date'))
    if start is not None and due is not None and int(due) < int(start):
        raise ValueError('Due date cannot be before planned start')
    if 'points' in opts:
        verify_delivery_parent(e, cfg, remote)
        spec_id(remote)
        if opts['points'] not in (1,2,3,5,8):
            raise ValueError('Sprint Points belong on a delivery unit and use Fibonacci 1,2,3,5,8')
        payload['points']=opts['points']
    add, remove = opts.get('assignee_add', []), opts.get('assignee_remove', [])
    if add or remove:
        if set(add) & set(remove):
            raise ValueError('Cannot add and remove the same assignee')
        payload['assignees'] = {'add': add, 'rem': remove}
    custom = []
    for item in opts.get('fields', []):
        key, sep, raw = item.partition('=')
        if not sep:
            raise ValueError('Use --field NAME=VALUE')
        f = field(remote, key)
        custom.append({'id': f['id'], 'name': f['name'], 'value': field_value(f, raw), 'type': f['type']})
    if not payload and not custom:
        raise ValueError('No update fields supplied')
    if apply:
        if payload:
            e.request('PUT', '/task/' + quote(remote['id']), e.token(), payload)
        for f in custom:
            body = {'value': f['value']}
            if f['type'] == 'date':
                body['value_options'] = {'time': True}
            e.request('POST', f'/task/{quote(remote["id"])}/field/{quote(f["id"])}', e.token(), body)
    return dict(identity(remote), apply=apply, changes=payload, custom_fields=custom)


def group_backlog(e, cfg, root, opts, apply):
    """Preserve delivery task identities while adding configured summary parents."""
    import time
    groups = cfg.get('groups', {})
    bindings = json.loads((root / 'Implementation/clickup-bindings.json').read_text())
    cards = {c['spec_id']: c for c in e.parse_cards(root)}
    planned = [sid for g in groups.values() for sid in g['cards']]
    if not groups or len(planned) != len(set(planned)) or set(planned) != set(cards) or set(bindings) != set(cards):
        raise ValueError('Groups must cover every bound plan card exactly once')
    assignee = opts.get('assignee')
    members = e.request('GET', '/list/' + cfg['list_id'] + '/member', e.token()).get('members', [])
    if len([m for m in members if m['id'] == assignee]) != 1:
        raise ValueError('Select one verified List member for summary parents')
    e.doctor(cfg, {cfg['statuses']['backlog']})
    tasks = e.all_tasks(cfg)
    indexed = {t['id']: t for t in tasks}
    existing, before, actions = {}, {}, []
    for key, group in groups.items():
        matches = [t for t in tasks if t['name'] == group['name'] and not t.get('parent')]
        marker = '[clickup-gh-workflow:group:' + key + ']'
        if len(matches) > 1 or (matches and marker not in description(matches[0])):
            raise ValueError('Ambiguous or unmanaged summary parent: ' + group['name'])
        existing[key] = matches[0] if matches else None
        for sid in group['cards']:
            unit = indexed.get(bindings[sid]['id'])
            expected = existing[key]['id'] if existing[key] else None
            if not unit or not unit['name'].startswith('[' + sid + '] ') or unit.get('parent') not in (None, expected):
                raise ValueError('Unexpected delivery unit identity/parent: ' + sid)
            children = [t for t in tasks if t.get('parent') == unit['id']]
            if sorted(t['name'] for t in children) != sorted(cards[sid]['subtasks']):
                raise ValueError('Step subtasks differ from the plan: ' + sid)
            for t in [unit, *children]:
                if str(t['list']['id']) != cfg['list_id'] or t.get('archived'):
                    raise ValueError('Cannot group archived or foreign-List tasks')
                before[t['id']] = t
        actions.append({'group': key, 'name': group['name'], 'action': 'reuse' if existing[key] else 'create', 'cards': group['cards']})
    prs = opts.get('prs')
    if prs is not None:
        if set(prs) != set(groups):
            raise ValueError('PR records must cover every configured group')
        for key, record in prs.items():
            parent = existing[key]
            pattern = r'https://github\.com/' + re.escape(cfg['repository']) + r'/pull/\d+'
            if not parent or record['id'] != parent['id'] or not record.get('verified') or not re.fullmatch(pattern,record['pr_url']):
                raise ValueError('Verified PR record does not match group: ' + key)
            if field(parent,'PR Link')['type'] != 'url':
                raise ValueError('PR Link must be a verified URL field')
    result_groups = {}
    moves = 0
    for key, group in groups.items():
        parent = existing[key]
        if apply and not parent:
            text = '[clickup-gh-workflow:group:' + key + ']\n\n'
            text += 'การ์ดหลักสำหรับรวมภาพงานในหมวดนี้ แต่ละงานย่อยที่มีรหัสสเปกใช้ branch/PR ของตัวเอง ส่วนขั้นตอนชั้นถัดไปอยู่ใน PR เดียวกับงานนั้น\n\n'
            text += 'Time Estimate อยู่ที่ขั้นตอนชั้นล่างสุด การ์ดหมวดและงานย่อยมี own estimate เป็น 0 เพื่อไม่รวมซ้ำ\n\n'
            text += '\n'.join('- [' + sid + ' / ' + bindings[sid]['custom_id'] + '](' + bindings[sid]['url'] + ')' for sid in group['cards'])
            parent = e.request('POST', '/list/' + cfg['list_id'] + '/task', e.token(),
                               {'name': group['name'], 'markdown_content': text, 'status': cfg['statuses']['backlog'],
                                'priority': 3, 'assignees': [assignee], 'time_estimate': 0, 'notify_all': False})
            print('Created summary ' + group['name'], flush=True)
            time.sleep(1)
        if parent:
            if prs is not None and apply:
                parent = e.request('GET', '/task/' + quote(parent['id']) + '?include_markdown_description=true', e.token())
                text = description(parent)
                text = text.replace('แต่ละงานย่อยที่มีรหัสสเปกใช้ branch/PR ของตัวเอง ส่วนขั้นตอนชั้นถัดไปอยู่ใน PR เดียวกับงานนั้น',
                                    'หนึ่งการ์ดหลักใช้หนึ่ง branch/PR ร่วมกัน งานย่อยและขั้นตอนทั้งหมดในหมวดใช้ PR ของการ์ดหลักตามที่ผู้ใช้ขอล่าสุด')
                begin,end='[clickup-gh-workflow:group-pr-begin]','[clickup-gh-workflow:group-pr-end]'
                if text.count(begin) != text.count(end) or text.count(begin)>1:
                    raise ValueError('Ambiguous PR section in group description')
                section=begin+'\nDraft PR: '+prs[key]['pr_url']+'\nBranch: '+prs[key]['branch']+'\nสถานะเริ่มต้น: เอกสารแผนสำหรับรีวิวขอบเขต ยังไม่ผ่าน RED/GREEN หรือการตรวจรับ\n'+end
                if begin in text:
                    text=text.split(begin)[0]+section+text.split(end)[1]
                else:
                    text+='\n\n'+section
                update(e,cfg,parent,{'description':text,'fields':['PR Link='+prs[key]['pr_url']]},True)
                parent=e.request('GET','/task/'+quote(parent['id'])+'?include_markdown_description=true',e.token())
                returned=description(parent)
                actual_section=returned.split(begin,1)[1].split(end,1)[0] if begin in returned and end in returned else ''
                if field(parent,'PR Link').get('value') != prs[key]['pr_url'] or prs[key]['pr_url'] not in actual_section or prs[key]['branch'] not in actual_section:
                    raise ValueError('Group PR link verification failed: '+key)
            for sid in group['cards']:
                unit = before[bindings[sid]['id']]
                if unit.get('parent') != parent['id']:
                    moves += 1
                    if apply:
                        e.request('PUT', '/task/' + quote(unit['id']), e.token(), {'parent': parent['id']})
                        check = e.request('GET', '/task/' + quote(unit['id']), e.token())
                        if check.get('parent') != parent['id']:
                            raise ValueError('Parent change did not persist; stop and inspect ' + sid)
                        print('Grouped ' + sid, flush=True)
                        time.sleep(1)
            if apply:
                parent = e.request('GET', '/task/' + quote(parent['id']), e.token())
            result_groups[key] = dict(identity(parent), name=group['name'], cards=group['cards'])
        else:
            moves += len(group['cards'])
            result_groups[key] = {'name': group['name'], 'cards': group['cards']}
    if apply:
        after = {t['id']: t for t in e.all_tasks(cfg)}
        for key, group in result_groups.items():
            actual = [t for t in after.values() if t.get('parent') == group['id']]
            if {t['id'] for t in actual} != {bindings[sid]['id'] for sid in group['cards']}:
                raise ValueError('Summary child verification failed: ' + key)
        stable = ('name','time_estimate','time_spent','points','due_date','start_date')
        for task_id, original in before.items():
            current = after.get(task_id)
            if not current or any(original.get(k) != current.get(k) for k in stable):
                raise ValueError('Existing task values changed; inspect ' + task_id)
            if e.status_name(original) != e.status_name(current) or original.get('priority') != current.get('priority'):
                raise ValueError('Status/priority changed; inspect ' + task_id)
            if {a['id'] for a in original.get('assignees', [])} != {a['id'] for a in current.get('assignees', [])}:
                raise ValueError('Assignee changed; inspect ' + task_id)
            if original.get('parent') in before and current.get('parent') != original.get('parent'):
                raise ValueError('Step subtask moved unexpectedly; inspect ' + task_id)
    return {'apply': apply, 'groups': result_groups, 'actions': actions, 'moves': moves, 'pr_links_verified': bool(prs is not None and apply),
            'delivery_units': len(cards), 'step_subtasks': len(before)-len(cards), 'verified': bool(apply)}


def assign_backlog(e, cfg, root, opts, apply):
    """Add a verified List member only to bound plan parents and direct subtasks."""
    import time
    assignee = opts.get('assignee')
    if not isinstance(assignee, int) or isinstance(assignee, bool) or assignee <= 0:
        raise ValueError('A positive numeric ClickUp member ID is required')
    members = e.request('GET', '/list/' + cfg['list_id'] + '/member', e.token()).get('members', [])
    matches = [m for m in members if str(m['id']) == str(assignee)]
    if len(matches) != 1:
        raise ValueError('Assignee must match exactly one verified List member')
    bindings = json.loads((root / 'Implementation/clickup-bindings.json').read_text())
    cards = e.parse_cards(root)
    if not cards or set(bindings) != {c['spec_id'] for c in cards}:
        raise ValueError('Every plan card must have a binding before bulk assignment')
    tasks = e.all_tasks(cfg)
    indexed = {t['id']: t for t in tasks}
    targets = []
    for card in cards:
        parent = indexed.get(bindings[card['spec_id']]['id'])
        if not parent or not parent['name'].startswith('[' + card['spec_id'] + '] '):
            raise ValueError('Bound parent identity mismatch: ' + card['spec_id'])
        verify_delivery_parent(e, cfg, parent)
        children = [t for t in tasks if t.get('parent') == parent['id']]
        if sorted(t['name'] for t in children) != sorted(card['subtasks']):
            raise ValueError('Subtasks differ from the plan: ' + card['spec_id'])
        targets.extend([parent, *children])
    if len({t['id'] for t in targets}) != len(targets):
        raise ValueError('Duplicate task identities in assignment scope')
    for t in targets:
        if str(t['list']['id']) != cfg['list_id'] or t.get('archived'):
            raise ValueError('Assignment scope contains archived or foreign-List tasks')
    def assigned(t):
        return any(str(a['id']) == str(assignee) for a in t.get('assignees', []))
    pending = [t for t in targets if not assigned(t)]
    if apply:
        for t in pending:
            update(e, cfg, t, {'assignee_add': [assignee]}, True)
            print('Assigned ' + str(t.get('custom_id') or t['id']), flush=True)
            time.sleep(1)  # Keep the batch below common ClickUp API rate limits.
        verified = {t['id']: t for t in e.all_tasks(cfg)}
        if any(t['id'] not in verified or not assigned(verified[t['id']]) for t in targets):
            raise ValueError('Assignment verification incomplete; inspect before retrying')
    return {'apply': apply, 'assignee': {k: matches[0].get(k) for k in ('id','username','email')},
            'parents': len(cards), 'subtasks': len(targets) - len(cards), 'total': len(targets),
            'changes': len(pending), 'already_assigned': len(targets) - len(pending),
            'verified': bool(apply), 'tasks': [{'id':t['id'], 'custom_id':t.get('custom_id')} for t in targets]}


def set_status(e, cfg, remote, opts, apply):
    available = [s['status'] for s in e.api_list(cfg)['statuses']]
    target = opts.get('to')
    if target is None:
        return dict(identity(remote), current=e.status_name(remote), available=available)
    matches = [s for s in available if s.casefold() == target.casefold()]
    if len(matches) != 1:
        raise ValueError('Status does not exist in this List. Available: ' + ', '.join(available))
    if apply and e.status_name(remote) != matches[0].casefold():
        e.request('PUT', '/task/' + quote(remote['id']), e.token(), {'status': matches[0]})
    return dict(identity(remote), apply=apply, before=e.status_name(remote), after=matches[0], reason=opts.get('reason'))


def estimate(e, cfg, root, remote, opts, apply):
    if 'duration_ms' not in opts:
        return e.set_subtask_estimates(cfg, card(e, root, spec_id(remote)), apply)
    value = int(opts['duration_ms'])
    if value < 0:
        raise ValueError('Estimate cannot be negative')
    children = [t for t in e.all_tasks(cfg) if t.get('parent') == remote['id']]
    if children and value:
        raise ValueError('Parent with subtasks must have zero own estimate; update its subtasks instead')
    if apply:
        e.request('PUT', '/task/' + quote(remote['id']), e.token(), {'time_estimate': value})
        result = e.request('GET', '/task/' + quote(remote['id']), e.token())
        if int(result.get('time_estimate') or 0) != value:
            raise ValueError('Estimate did not persist')
    return dict(identity(remote), apply=apply, before=remote.get('time_estimate'), after=value,
                note='Single-task edit does not silently rewrite the local spec allocation; use diff to reconcile the plan.')


def diff(e, root, remote, cfg=None):
    local = card(e, root, spec_id(remote))
    text = description(remote)
    starts, ends = text.count(BEGIN), text.count(END)
    if starts != ends or starts > 1:
        raise ValueError('Ambiguous managed description markers')
    managed = text.split(BEGIN, 1)[1].split(END, 1)[0].strip() if starts else None
    if starts and text.index(END) < text.index(BEGIN):
        raise ValueError('Invalid marker order')
    digest = hashlib.sha256((remote['name'] + '\n' + text).encode()).hexdigest()
    desired = local['description'].strip()
    changes = []
    if remote['name'] != local['name']:
        changes.append('name')
    if managed != desired:
        changes.append('managed_description')
    estimates = []
    if cfg is not None:
        children = [t for t in e.all_tasks(cfg) if t.get('parent') == remote['id']]
        for title, planned in zip(local['subtasks'], local['subtask_estimates_ms']):
            found = [t for t in children if t['name'] == title]
            actual = found[0].get('time_estimate') if len(found) == 1 else None
            if len(found) != 1 or int(actual or 0) != planned:
                estimates.append({'subtask': title, 'planned_ms': planned, 'actual_ms': actual, 'matches': len(found)})
    patch = '\n'.join(difflib.unified_diff((managed or '').splitlines(), desired.splitlines(), fromfile='ClickUp managed spec', tofile=local['spec'], lineterm=''))
    return dict(identity(remote), spec_id=local['spec_id'], changes=changes, remote_sha=digest,
                requires_adoption=managed is None, diff=patch, estimate_differences=estimates, local_name=local['name'],
                protected_fields=['status', 'priority', 'assignees', 'start_date', 'due_date', 'tracked_time'],
                estimate_note='Header estimates are planned totals. Runtime estimates are changed only by estimate commands.')


def sync(e, cfg, root, remote, opts, apply):
    result = diff(e, root, remote, cfg)
    local = card(e, root, result['spec_id'])
    if result['requires_adoption'] and not opts.get('adopt'):
        if apply:
            raise ValueError('Legacy/unmanaged description: preview diff, then use sync --adopt to preserve existing notes and add a managed section')
        return dict(result, apply=False, note='Use --adopt for initial managed section; existing description will be preserved')
    expected = opts.get('expected_remote_sha')
    if apply and expected != result['remote_sha']:
        raise ValueError('Remote task changed or diff baseline missing; run diff again before applying sync')
    snapshot = BEGIN + '\n' + local['description'].strip() + '\n' + END
    old = description(remote)
    if result['requires_adoption']:
        merged = old.rstrip() + '\n\n' + snapshot
    else:
        before, tail = old.split(BEGIN, 1)
        _, after = tail.split(END, 1)
        merged = before + snapshot + after
    if apply and result['changes']:
        e.request('PUT', '/task/' + quote(remote['id']), e.token(), {'name': local['name'], 'markdown_content': merged})
        refreshed = task(e, cfg, remote['id'])
        result['remote_sha'] = diff(e, root, refreshed)['remote_sha']
    return dict(result, apply=apply, note='Only name/managed spec are synced; manual notes, status, schedule, priority and actual time are preserved')


def time_entries(e, cfg, remote, opts, apply):
    query = {'task_id': remote['id']}
    for source, dest in (('from', 'start_date'), ('until', 'end_date')):
        if opts.get(source):
            query[dest] = timestamp(opts[source])
    if opts.get('assignee') is not None:
        query['assignee']=int(opts['assignee'])
    path = '/team/' + cfg['workspace_id'] + '/time_entries'
    if opts.get('mode') == 'list':
        return dict(identity(remote), entries=e.request('GET', path + '?' + urlencode(query), e.token()).get('data', []))
    if opts.get('mode') in ('edit','delete'):
        entry_id = str(opts.get('entry_id',''))
        if not re.fullmatch(r'\d+',entry_id):
            raise ValueError('A single numeric time entry ID is required')
        entry_path = path + '/' + entry_id
        current = e.request('GET',entry_path,e.token()).get('data',{})
        if str((current.get('task') or {}).get('id')) != str(remote['id']):
            raise ValueError('Time entry is not associated with the selected task')
        if opts['mode']=='delete':
            if not opts.get('reason'): raise ValueError('Deleting a time entry requires a reason')
            if apply: e.request('DELETE',entry_path,e.token())
            return dict(identity(remote),apply=apply,action='delete',entry_id=entry_id,before=current,reason=opts['reason'])
        payload={'tags':[],'tag_action':'add'}
        if 'note' in opts:
            markers = re.findall(r'\[clickup-gh-workflow:[a-f0-9-]+\]',current.get('description') or '')
            payload['description']='\n'.join([opts['note'],*[m for m in markers if m not in opts['note']]]).strip()
        if 'duration_ms' in opts:
            if int(opts['duration_ms'])<=0: raise ValueError('Duration must be positive')
            payload['duration']=int(opts['duration_ms'])
        if 'start_ms' in opts:
            length=int(opts.get('duration_ms',current.get('duration') or 0))
            if length<=0: raise ValueError('Cannot edit a running/invalid entry')
            payload.update(start=int(opts['start_ms']),end=int(opts['start_ms'])+length)
        if len(payload)==2: raise ValueError('No time entry changes supplied')
        if apply: e.request('PUT',entry_path,e.token(),payload)
        return dict(identity(remote),apply=apply,action='edit',entry_id=entry_id,before=current,changes=payload)
    start, length = int(opts['start_ms']), int(opts['duration_ms'])
    request_id = opts['request_id']
    if length <= 0 or not re.fullmatch(r'[a-f0-9-]+', request_id):
        raise ValueError('Invalid duration or request identity')
    marker = f'[clickup-gh-workflow:{request_id}]'
    query.update(start_date=start - 86400000, end_date=start + length + 86400000)
    existing = e.request('GET', path + '?' + urlencode(query), e.token()).get('data', [])
    matches = [entry for entry in existing if marker in (entry.get('description') or '')]
    if len(matches) > 1:
        raise ValueError('Duplicate time entries with this request ID; inspect manually')
    payload = {'tid': remote['id'], 'start': start, 'duration': length,
               'description': (opts.get('note', '') + '\n' + marker).strip(), 'billable': bool(opts.get('billable'))}
    if opts.get('assignee') is not None:
        payload['assignee'] = int(opts['assignee'])
    response = None
    if apply and not matches:
        response = e.request('POST', path, e.token(), payload)
    return dict(identity(remote), apply=apply, action='existing' if matches else 'create',
                entry=matches[0] if matches else (response or {}).get('data'), payload=payload,
                note='Entries belong to the token owner unless an authorized assignee was explicitly selected')


def resolve_start(e, cfg, root, remote):
    verify_delivery_parent(e, cfg, remote)
    if e.status_name(remote) != cfg['statuses']['ready']:
        raise ValueError('Task is not READY; Lead approval is required')
    if not remote.get('custom_id'):
        raise ValueError('Custom DEV ID is missing; read identity again first')
    local = card(e, root, spec_id(remote))
    deps = re.search(r'\*\*Dependency:\*\* (.+)', local['description'])
    ids = re.findall(e.TASK_ID, deps[1]) if deps else []
    tasks = e.all_tasks(cfg) if ids else []
    dependencies = []
    for sid in ids:
        matches = [t for t in tasks if t['name'].startswith(f'[{sid}]')]
        if len(matches) != 1 or not matches[0].get('custom_id'):
            raise ValueError('Dependency lacks an unambiguous DEV binding: ' + sid)
        dependencies.append(dict(identity(matches[0]), spec_id=sid))
    for dep in remote.get('dependencies', []):
        if dep.get('task_id') == remote['id'] and dep.get('depends_on') not in {d['id'] for d in dependencies}:
            raise ValueError('Unresolved native ClickUp dependency; bind it in the spec before start')
    return dict(identity(remote), spec_id=local['spec_id'], spec=local['spec'], name=local['name'], dependencies=dependencies)


def pr_metadata(e, cfg, remote, opts, apply):
    pattern = r'https://github\.com/' + re.escape(cfg['repository']) + r'/pull/\d+'
    if not re.fullmatch(pattern, opts['url']) or opts['base'] != cfg['base_branch']:
        raise ValueError('PR repository/base does not match project config')
    custom = cfg.get('fields', {})
    values = [('pr_link', 'PR Link', 'url', opts['url']),
              ('pr_started_at', 'Start Date', 'date', timestamp(opts['created_at']))]
    if opts.get('merged_at'):
        values.append(('pr_merged_at', 'End Date', 'date', timestamp(opts['merged_at'])))
    pending, updates = [], []
    for key, label, kind, value in values:
        try:
            f = field(remote, custom.get(key, label))
        except ValueError:
            pending.append(key)
            continue
        if f['type'] != kind:
            pending.append(key)
            continue
        updates.append({'id': f['id'], 'name': f['name'], 'value': value, 'type': kind})
    if apply:
        for update in updates:
            body = {'value': update['value']}
            if update['type'] == 'date':
                body['value_options'] = {'time': True}
            e.request('POST', f'/task/{quote(remote["id"])}/field/{quote(update["id"])}', e.token(), body)
    state_result = None
    if not opts.get('merged_at') and opts.get('state') == 'OPEN':
        allowed = {cfg['statuses'][key] for key in ('ready', 'in_progress', 'in_review', 'rework')}
        if e.status_name(remote) in allowed:
            target = cfg['statuses']['in_progress' if opts['draft'] else 'in_review']
            state_result = set_status(e, cfg, remote, {'to': target}, apply)
    return dict(identity(remote), spec_id=spec_id(remote), apply=apply, fields=updates, pending_field_mapping=pending, status=state_result,
                note='PR timestamps are separate custom fields; planned start/due are preserved. Merge never means QA-ready or DONE automatically.')


def _member_summary(member):
    user = member.get('user', member)
    return {k: user.get(k) for k in ('id', 'username', 'email')}


def resolve_member(members, selector):
    """Match a List member by numeric ID, email or username; never guess."""
    people = [_member_summary(m) for m in members]
    wanted = str(selector).strip()
    if not wanted:
        raise ValueError('Assignee selector is empty')
    folded = wanted.casefold()
    matches = [p for p in people if str(p['id']) == wanted]
    if not matches:
        matches = [p for p in people if (p.get('email') or '').casefold() == folded]
    if not matches:
        matches = [p for p in people if (p.get('username') or '').casefold() == folded]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError('Assignee "' + wanted + '" matches several List members; use the numeric ID: '
                         + ', '.join(f"{p['username']} ({p['id']})" for p in matches))
    names = [p.get('username') or '' for p in people]
    close = difflib.get_close_matches(wanted, names, n=3, cutoff=0.5)
    hint = (' Did you mean: ' + ', '.join(close) + '?') if close else ''
    raise ValueError('No member of this ClickUp List matches "' + wanted + '".' + hint
                     + ' Run members to see valid IDs, then pass --assignee <id|email|username>.')


def list_tasks(e, cfg, root, opts):
    """Read-only listing of List tasks, optionally filtered to one verified member."""
    opts = opts or {}
    selector, mine = opts.get('assignee'), bool(opts.get('mine'))
    person, source = None, None
    if selector is not None or mine:
        members = e.request('GET', '/list/' + cfg['list_id'] + '/member', e.token()).get('members', [])
        if selector is not None:
            person, source = resolve_member(members, selector), opts.get('assignee_source', 'argument')
        else:
            current = e.request('GET', '/user', e.token()).get('user') or {}
            matches = [_member_summary(m) for m in members if str(_member_summary(m)['id']) == str(current.get('id'))]
            if len(matches) != 1:
                raise ValueError('Cannot tell who "mine" is: the ClickUp token belongs to '
                                 + str(current.get('username') or current.get('id') or 'an unknown user')
                                 + ', who is not a member of this List. Ask the user which ClickUp member they are '
                                 + 'and pass --assignee <id|email|username> (add --remember to save it locally).')
            person, source = matches[0], 'token-owner'
    bindings_path = root / 'Implementation/clickup-bindings.json'
    bound = {}
    if bindings_path.exists():
        bound = {v['id']: k for k, v in json.loads(bindings_path.read_text()).items() if isinstance(v, dict) and v.get('id')}
    rows = []
    for t in e.all_tasks(cfg):
        if t.get('archived') or str((t.get('list') or {}).get('id', cfg['list_id'])) != cfg['list_id']:
            continue
        status = t.get('status') or {}
        if not opts.get('include_closed') and status.get('type') in ('closed', 'done'):
            continue
        if person and not any(str(a.get('id')) == str(person['id']) for a in t.get('assignees', [])):
            continue
        match = re.match(r'^\[([A-Z][A-Z0-9]*-\d+)\]', t.get('name', ''))
        priority = t.get('priority') or {}
        rows.append({'id': t['id'], 'custom_id': t.get('custom_id'), 'name': t.get('name'),
                     'spec_id': bound.get(t['id']) or (match[1] if match else None),
                     'status': status.get('status'), 'priority': priority.get('priority'),
                     'due_date': t.get('due_date'), 'start_date': t.get('start_date'),
                     'time_estimate': t.get('time_estimate'), 'time_spent': t.get('time_spent'),
                     'parent': t.get('parent'), 'url': t.get('url'),
                     'assignees': [a.get('username') for a in t.get('assignees', [])]})
    rows.sort(key=lambda r: (int(r['due_date']) if r['due_date'] else float('inf'), r['custom_id'] or r['id']))
    counts = {}
    for r in rows:
        counts[r['status']] = counts.get(r['status'], 0) + 1
    result = {'assignee': person, 'assignee_source': source, 'include_closed': bool(opts.get('include_closed')),
              'total': len(rows), 'by_status': counts, 'tasks': rows}
    if source == 'token-owner':
        result['note'] = ('"mine" was resolved from the ClickUp token owner. If this token is shared '
                          '(for example one GitHub Actions secret for the team), confirm this is you or pass --assignee.')
    return result


def dispatch(e, cfg, root, operation, ident, opts=None, apply=False):
    opts = opts or {}
    remote = task(e, cfg, ident)
    if operation == 'read':
        fields = ('id','custom_id','name','url','description','markdown_description','status','priority','assignees','start_date','due_date','time_estimate','time_spent','parent','dependencies','subtasks','custom_fields')
        return {key: remote.get(key) for key in fields}
    if operation == 'update': return update(e, cfg, remote, opts, apply)
    if operation == 'status': return set_status(e, cfg, remote, opts, apply)
    if operation == 'estimate': return estimate(e, cfg, root, remote, opts, apply)
    if operation == 'diff': return diff(e, root, remote, cfg)
    if operation == 'sync': return sync(e, cfg, root, remote, opts, apply)
    if operation in ('time-list','time-log','time-edit','time-delete'):
        return time_entries(e,cfg,remote,dict(opts,mode=operation.split('-',1)[1]),apply)
    if operation == 'resolve-start': return resolve_start(e, cfg, root, remote)
    if operation == 'pr-metadata': return pr_metadata(e, cfg, remote, opts, apply)
    raise ValueError('Unsupported operation')
