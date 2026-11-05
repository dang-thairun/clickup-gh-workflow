#!/usr/bin/env python3
"""Install the standalone CLI and optional shared AI Skill. Preview by default."""
import argparse
import json
from pathlib import Path
import shlex
import shutil
import sys

if sys.version_info < (3, 11):
    raise SystemExit('Python 3.11+ is required; run with python3.11')
SOURCE = Path(__file__).resolve().parents[1]
MARKER = 'clickup-gh-workflow-managed-v1'


def install(prefix, skill_dir, apply=False):
    prefix=prefix.expanduser().resolve(); skill_dir=skill_dir.expanduser().resolve()
    share=prefix/'share/clickup-gh-workflow'; entry=prefix/'bin/clickup-gh-workflow'
    for directory in (share,skill_dir):
        marker=directory/'.managed-by'
        if directory.exists() and (not marker.exists() or marker.read_text().strip()!=MARKER):
            raise ValueError('Existing directory is not owned by this installer: '+str(directory))
    if entry.exists() and '# '+MARKER not in entry.read_text():
        raise ValueError('Existing command belongs to another installer; refusing to overwrite')
    if apply:
        shutil.copytree(SOURCE/'src/clickup_gh_workflow',share,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        (share/'.managed-by').write_text(MARKER+'\n')
        if (SOURCE/'README.md').exists():shutil.copy2(SOURCE/'README.md',share/'README.md')
        entry.parent.mkdir(parents=True,exist_ok=True)
        entry.write_text('#!/bin/sh\n# '+MARKER+'\nset -eu\nexec '+shlex.quote(sys.executable)+' '+shlex.quote(str(share/'run.py'))+' "$@"\n')
        entry.chmod(0o755)
        skill_dir.mkdir(parents=True,exist_ok=True)
        shutil.copy2(SOURCE/'src/clickup_gh_workflow/skill/SKILL.md',skill_dir/'SKILL.md')
        (skill_dir/'.managed-by').write_text(MARKER+'\n')
    return {'apply':apply,'command':str(entry),'runtime':str(share),'skill':str(skill_dir/'SKILL.md'),
            'note':'No credentials, shell startup files or AI provider configuration are modified.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prefix',type=Path,default=Path.home()/'.local')
    parser.add_argument('--skill-dir',type=Path,default=Path.home()/'.agents/skills/clickup-gh-workflow')
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    try:print(json.dumps(install(args.prefix,args.skill_dir,args.apply),indent=2))
    except (ValueError,OSError) as exc:
        print('Error: '+str(exc),file=sys.stderr);sys.exit(1)
