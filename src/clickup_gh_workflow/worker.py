"""GitHub Actions worker; only runs the operation encoded in request.json."""
import importlib.util
import json
import sys
from pathlib import Path


def load_engine(path):
    spec = importlib.util.spec_from_file_location('workflow_engine', path)
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    return engine


def execute(engine, cfg, root, operation, ident, apply=False, options=None):
    if apply and operation not in ('group-backlog','assign-backlog','create-card','set-estimates','update','estimate','status','sync','time-log','time-edit','time-delete','pr-metadata'):
        raise ValueError('Read-only operations do not accept apply')
    if operation == 'doctor':
        return engine.doctor(cfg)
    if operation == 'members':
        members = engine.request('GET', '/list/' + cfg['list_id'] + '/member', engine.token())
        current = engine.request('GET', '/user', engine.token())
        return {'members': members.get('members', []), 'current_user': current.get('user')}
    if operation == 'tasks':
        extension = load_engine(Path(__file__).with_name('operations.py'))
        return extension.list_tasks(engine, cfg, root, options or {})
    if operation == 'assign-backlog':
        extension = load_engine(Path(__file__).with_name('operations.py'))
        return extension.assign_backlog(engine, cfg, root, options or {}, apply)
    if operation == 'group-backlog':
        extension = load_engine(Path(__file__).with_name('operations.py'))
        return extension.group_backlog(engine, cfg, root, options or {}, apply)
    if operation in ('create-card', 'set-estimates'):
        cards = [c for c in engine.parse_cards(root) if c['spec_id'] == ident]
        if len(cards) != 1:
            raise ValueError('Exactly one spec card must match; no bulk import is performed')
        if operation == 'set-estimates':
            return engine.set_subtask_estimates(cfg, cards[0], apply)
        if 'description' in cards[0]:
            cards[0] = dict(cards[0], description='[clickup-gh-workflow:spec-begin]\n' + cards[0]['description'] + '\n[clickup-gh-workflow:spec-end]')
        if (options or {}).get('priority') is not None:
            cfg = dict(cfg, defaults=dict(cfg.get('defaults', {}), priority=options['priority']))
        result = engine.create_cards(cfg, cards, root, apply)
        # Re-read the existing task, including delayed custom IDs, without creating again.
        if apply:
            bindings_path = root / 'Implementation/clickup-bindings.json'
            bindings = json.loads(bindings_path.read_text())
            binding = bindings[ident]
            identity = engine.inspect_task(cfg, binding['id'])
            if identity['custom_id']:
                binding['custom_id'] = identity['custom_id']
                engine.write_json(bindings_path, bindings)
            result['identity'] = identity
        return result
    if operation == 'read-card':
        if engine.re.fullmatch(engine.custom_id_pattern(cfg), ident):
            internal_id = engine.get_task(cfg, ident)['id']
        elif engine.re.fullmatch(engine.TASK_ID, ident):
            matches = [t for t in engine.all_tasks(cfg) if t['name'].startswith(f'[{ident}]')]
            if len(matches) != 1:
                raise ValueError('Spec ID must identify exactly one existing task')
            internal_id = matches[0]['id']
        else:
            internal_id = ident
        return engine.inspect_task(cfg, internal_id)
    extension = load_engine(Path(__file__).with_name('operations.py'))
    return extension.dispatch(engine, cfg, root, operation, ident, options, apply)


def main():
    payload = Path(__file__).resolve().parent
    request = json.loads((payload / 'request.json').read_text())
    engine = load_engine(payload / 'engine.py')
    root = payload / 'project'
    cfg = engine.tomllib.loads((root / '.clickup-workflow.toml').read_text())
    try:
        value = execute(engine, cfg, root, request['operation'], request.get('id'), request['apply'], request.get('options'))
        result = {'success': True, 'operation': request['operation'], 'result': value}
    except Exception as exc:
        result = {'success': False, 'operation': request['operation'], 'error': str(exc),
                  'note': 'A failed write may have partially completed. Read remote state before rerunning.'}
    (payload / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    # Result fields contain task metadata, never request headers or credentials.
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['success'] else 1


if __name__ == '__main__':
    sys.exit(main())
