#!/usr/bin/env python3
"""Agent-independent ClickUp workflow. Requires Python 3.11+, git and GitHub CLI."""
import sys
import os
import shutil
if sys.version_info < (3, 11):
    interpreter = shutil.which('python3.11')
    if interpreter:
        os.execv(interpreter, [interpreter, *sys.argv])
    raise SystemExit('Python 3.11+ is required.')
import argparse
import importlib.util
import json
import re
import subprocess
import tempfile
import time
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

HERE = Path(__file__).resolve().parent


def engine_path():
    return HERE / 'engine.py'


def engine():
    spec = importlib.util.spec_from_file_location('workflow_engine', engine_path())
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def command(args, cwd=None, timeout=90):
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=timeout)
    if result.returncode:
        # Avoid logging arbitrary credential-helper output; gh does not expose ClickUp secrets.
        raise ValueError(f'Command failed: {args[0]} {args[1]} (exit {result.returncode}); inspect receipt before retrying')
    return result.stdout


def gh_json(path):
    return json.loads(command(['gh', 'api', path]))


def validate(cfg, operation, ident, apply):
    for key in ('repository', 'fork'):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', cfg[key]):
            raise ValueError(f'Invalid GitHub repository: {key}')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_./-]*', cfg['base_branch']):
        raise ValueError('Invalid base branch')
    if operation not in ('doctor','members','assign-backlog','group-backlog','create-card','read-card','set-estimates','read','update','estimate','status','diff','sync','time-list','time-log','time-edit','time-delete','resolve-start','pr-metadata'):
        raise ValueError('Unsupported operation')
    if operation not in ('doctor','members','assign-backlog','group-backlog') and not re.fullmatch(r'[A-Za-z0-9_]+(?:-[0-9]+)?', ident or ''):
        raise ValueError('Expected a spec, configured custom ID or internal task ID')
    if operation in ('create-card', 'set-estimates') and not re.fullmatch(r'[A-Z][A-Z0-9]*-\d+', ident):
        raise ValueError('A spec ID is required for writes')
    if apply and operation not in ('group-backlog','assign-backlog','create-card','set-estimates','update','estimate','status','sync','time-log','time-edit','time-delete','pr-metadata'):
        raise ValueError('--apply is not valid for this read-only operation')


def workflow_text(cfg, branch, payload, artifact):
    # Every dynamic YAML scalar is JSON-quoted; no user input is interpolated into shell.
    return f'''name: Team workflow operation
on:
  push:
    branches: [{json.dumps(branch)}]
permissions:
  contents: read
concurrency:
  group: team-workflow-{cfg['list_id']}
  cancel-in-progress: false
jobs:
  operate:
    if: github.repository == '{cfg['fork']}'
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683
        with:
          persist-credentials: false
      - uses: actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065
        with:
          python-version: '3.11'
      - name: Execute scoped request
        env:
          CLICKUP_API_TOKEN: ${{{{ secrets.CLICKUP_API_TOKEN }}}}
        run: python3 {payload}/worker.py
      - name: Save result (including partial failures)
        if: always()
        uses: actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02
        with:
          name: {artifact}
          path: |
            {payload}/result.json
            {payload}/project/Implementation/clickup-bindings.json
          if-no-files-found: ignore
          retention-days: 7
'''


def save_receipt(path, receipt):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(receipt, indent=2) + '\n')


def prepare(root, operation, ident, apply, prepare_only=False, options=None):
    cfg = tomllib.loads((root / '.clickup-workflow.toml').read_text())
    validate(cfg, operation, ident, apply)
    if not re.fullmatch(r'\d+', cfg['list_id']):
        raise ValueError('Expected a numeric ClickUp List ID')
    if operation in ('create-card', 'set-estimates'):
        matches = [c for c in engine().parse_cards(root) if c['spec_id'] == ident]
        if len(matches) != 1:
            raise ValueError('No unique spec card; nothing was pushed')
    request_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    branch = 'automation/clickup-' + request_id
    payload = '.team-workflow-runs/' + request_id
    artifact = 'team-workflow-' + request_id
    temp = Path(tempfile.mkdtemp(prefix='clickup-gh-workflow-'))
    project = temp / payload / 'project'
    (project / 'Implementation').mkdir(parents=True)
    shutil.copy2(root / '.clickup-workflow.toml', project / '.clickup-workflow.toml')
    for spec in (root / 'Implementation').glob('*-spec.md'):
        shutil.copy2(spec, project / 'Implementation' / spec.name)
    bindings = root / 'Implementation/clickup-bindings.json'
    if bindings.exists():
        shutil.copy2(bindings, project / 'Implementation/clickup-bindings.json')
    shutil.copy2(engine_path(), temp / payload / 'engine.py')
    shutil.copy2(HERE / 'worker.py', temp / payload / 'worker.py')
    shutil.copy2(HERE / 'operations.py', temp / payload / 'operations.py')
    (temp / payload / 'request.json').write_text(json.dumps({'operation': operation, 'id': ident, 'apply': apply, 'options': options or {}}))
    workflow = temp / '.github/workflows' / ('team-workflow-' + request_id + '.yml')
    workflow.parent.mkdir(parents=True)
    workflow.write_text(workflow_text(cfg, branch, payload, artifact))
    receipt = {'request_id': request_id, 'repository': cfg['fork'], 'branch': branch,
               'operation': operation, 'id': ident, 'apply': apply, 'artifact': artifact,
               'project': str(root), 'staging': str(temp), 'state': 'prepared', 'options': options or {}}
    receipt_path = root / '.team-workflow/receipts' / (request_id + '.json')
    save_receipt(receipt_path, receipt)
    print('Receipt: ' + str(receipt_path), flush=True)
    if prepare_only:
        return receipt_path, receipt
    permissions = gh_json('repos/' + cfg['fork']).get('permissions', {})
    if not permissions.get('push'):
        raise ValueError('GitHub CLI account cannot push to the configured fork')
    # Isolated staging checkout. Never switch, stage or commit the user's working tree.
    command(['git', 'init', '--quiet'], temp)
    command(['git', 'remote', 'add', 'origin', 'https://github.com/' + cfg['fork'] + '.git'], temp)
    command(['git', '-c', 'credential.helper=!gh auth git-credential', 'fetch', '--depth=1', 'origin', cfg['base_branch']], temp)
    # read-tree seeds the index without checking out files or executing project code/hooks.
    command(['git', 'read-tree', 'FETCH_HEAD'], temp)
    command(['git', 'add', '--', payload, str(workflow.relative_to(temp))], temp)
    tree = command(['git', 'write-tree'], temp).strip()
    parent = command(['git', 'rev-parse', 'FETCH_HEAD'], temp).strip()
    sha = command(['git', '-c', 'user.name=Team Workflow', '-c', 'user.email=team-workflow@localhost',
                   'commit-tree', tree, '-p', parent, '-m', f'workflow: {operation} {ident or ""}'], temp).strip()
    receipt.update(head_sha=sha, state='push-pending')
    save_receipt(receipt_path, receipt)
    command(['git', '-c', 'credential.helper=!gh auth git-credential', 'push', 'origin', sha + ':refs/heads/' + branch], temp)
    receipt['state'] = 'pushed'
    save_receipt(receipt_path, receipt)
    return receipt_path, receipt


def record_metadata(root, value, options):
    # PR lifecycle bookkeeping is local runtime state, not a feature-owned source edit.
    path = root / '.team-workflow/pr-metadata.json'
    records = json.loads(path.read_text()) if path.exists() else {}
    key = value.get('custom_id') or value['id']
    records[key] = {'id':value['id'],'spec_id':value.get('spec_id'),
                    'pr':{field:options.get(field) for field in ('url','created_at','merged_at','state','draft')},
                    'pending_clickup_fields':value.get('pending_field_mapping',[])}
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(records,ensure_ascii=False,indent=2)+'\n')


def finish(receipt_path, receipt, timeout, emit=True):
    deadline = time.monotonic() + timeout
    repo = receipt['repository']
    while not receipt.get('run_id'):
        query = urlencode({'branch': receipt['branch'], 'per_page': 10})
        runs = gh_json(f'repos/{repo}/actions/runs?{query}')['workflow_runs']
        matching = [r for r in runs if r['head_sha'] == receipt.get('head_sha') and r['name'] == 'Team workflow operation']
        if len(matching) > 1:
            raise ValueError('Multiple matching runs; inspect receipt instead of replaying')
        if matching:
            receipt.update(run_id=matching[0]['id'], run_url=matching[0]['html_url'])
            save_receipt(receipt_path, receipt)
            print('Actions: ' + receipt['run_url'], flush=True)
            break
        if time.monotonic() >= deadline:
            raise ValueError('Run not found yet; resume this receipt, do not rerun the write')
        time.sleep(3)
    while True:
        run = gh_json(f'repos/{repo}/actions/runs/{receipt["run_id"]}')
        if run['status'] == 'completed':
            break
        if time.monotonic() >= deadline:
            raise ValueError('Workflow still running; resume this receipt')
        time.sleep(5)
    receipt['conclusion'] = run['conclusion']
    destination = receipt_path.with_suffix('')
    destination.mkdir(exist_ok=True)
    results = list(destination.rglob('result.json'))
    if not results:
        command(['gh', 'run', 'download', str(receipt['run_id']), '--repo', repo,
                 '--name', receipt['artifact'], '--dir', str(destination)], timeout=90)
        results = list(destination.rglob('result.json'))
    if len(results) != 1:
        raise ValueError('No unique result artifact; inspect Actions logs')
    result = json.loads(results[0].read_text())
    if result.get('success') and run['conclusion'] == 'success':
        binding_files = destination.rglob('clickup-bindings.json') if receipt['operation'] in ('create-card','read-card','read') else []
        for binding_file in binding_files:
            new = json.loads(binding_file.read_text())
            target = Path(receipt['project']) / 'Implementation/clickup-bindings.json'
            current = json.loads(target.read_text()) if target.exists() else {}
            current.update(new)
            target.parent.mkdir(exist_ok=True)
            target.write_text(json.dumps(current, ensure_ascii=False, indent=2) + '\n')
        value = result['result']
        if receipt['operation'] == 'group-backlog' and receipt['apply']:
            target = Path(receipt['project']) / 'Implementation/clickup-groups.json'
            target.write_text(json.dumps(value['groups'], ensure_ascii=False, indent=2) + '\n')
        if receipt['operation'] in ('read-card','read') and value.get('custom_id'):
            target = Path(receipt['project']) / 'Implementation/clickup-bindings.json'
            current = json.loads(target.read_text()) if target.exists() else {}
            for binding in current.values():
                if binding['id'] == value['id']:
                    binding['custom_id'] = value['custom_id']
            spec_match = re.match(r'^\[([A-Z][A-Z0-9]*-\d+)\]', value['name'])
            if spec_match:
                spec_id = spec_match[1]
                for spec_file in (Path(receipt['project']) / 'Implementation').glob('*-spec.md'):
                    if f'## [{spec_id}] ' in spec_file.read_text():
                        current[spec_id] = {'id': value['id'], 'custom_id': value['custom_id'], 'url': value.get('url'),
                                            'spec': str(spec_file.relative_to(receipt['project']))}
                        break
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(current, ensure_ascii=False, indent=2) + '\n')
        if receipt['operation']=='pr-metadata':
            record_metadata(Path(receipt['project']),value,receipt.get('options',{}))
        if receipt['operation'] == 'time-log':
            timer_path = Path(receipt['project']) / '.team-workflow/timer.json'
            if timer_path.exists():
                timer = json.loads(timer_path.read_text())
                active = timer.get('active')
                if active and active.get('request_id') == receipt.get('options', {}).get('request_id') and active.get('state') == 'pending-stop':
                    timer_path.write_text(json.dumps({'active': None, 'last_completed': active}, indent=2) + '\n')
        receipt['state'] = 'completed'
    else:
        receipt['state'] = 'failed-inspect-before-retry'
    save_receipt(receipt_path, receipt)
    if emit:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if receipt['state'] != 'completed':
        raise ValueError('Remote operation failed; partial changes may exist. Read state before retrying')
    return result


def legacy_main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest='mode', required=True)
    sub.add_parser('plan', help='Read local specs offline')
    local = sub.add_parser('local', help='Pass commands to the API CLI; requires local token')
    local.add_argument('args', nargs=argparse.REMAINDER)
    github = sub.add_parser('github', help='Run using the fork Actions secret; token stays on GitHub')
    github.add_argument('operation', choices=['doctor', 'create-card', 'read-card', 'set-estimates'])
    github.add_argument('id', nargs='?')
    github.add_argument('--apply', action='store_true')
    github.add_argument('--prepare-only', action='store_true', help='Only generate files locally; no GitHub calls or push')
    github.add_argument('--timeout', type=int, default=600)
    resume = sub.add_parser('resume', help='Resume observing a previous receipt; never repeats a write')
    resume.add_argument('receipt', type=Path)
    resume.add_argument('--timeout', type=int, default=600)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if args.mode == 'plan':
        engine().main(['--root', str(root), 'plan'])
    elif args.mode == 'local':
        engine().main(['--root', str(root), *args.args])
    elif args.mode == 'resume':
        path = args.receipt.resolve()
        receipt = json.loads(path.read_text())
        if receipt['state'] == 'prepared':
            raise ValueError('Prepared-only receipt was never pushed')
        finish(path, receipt, args.timeout)
    else:
        path, receipt = prepare(root, args.operation, args.id, args.apply, args.prepare_only)
        if args.prepare_only:
            print(json.dumps(receipt, indent=2))
        else:
            finish(path, receipt, args.timeout)


def main(argv=None):
    spec = importlib.util.spec_from_file_location('workflow_frontend', HERE / 'frontend.py')
    frontend = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(frontend)
    return frontend.main(sys.argv[1:] if argv is None else argv, globals())


def cli():
    try:
        main()
        return 0
    except (ValueError, OSError, KeyError, subprocess.TimeoutExpired) as exc:
        print('Error: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(cli())
