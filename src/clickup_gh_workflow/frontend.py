"""Unified clickup-gh-workflow command interface."""
import argparse
import importlib.util
import json
import re
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent


def module(name):
    spec = importlib.util.spec_from_file_location('workflow_' + name, HERE / (name + '.py'))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def add_run_flags(parser, mutate=False):
    if mutate: parser.add_argument('--apply', action='store_true')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--timeout', type=int, default=600)


def parser():
    p = argparse.ArgumentParser(prog='clickup-gh-workflow', description='ClickUp tasks, time and GitHub PR delivery. Writes preview by default; use --apply.')
    p.add_argument('--version',action='version',version='clickup-gh-workflow 1.0.4')
    p.add_argument('--root', type=Path, default=Path.cwd())
    p.add_argument('--via', choices=['github', 'local'], default='github', help='GitHub keeps ClickUp token in Actions; local uses environment token')
    sub = p.add_subparsers(dest='cmd', required=True)
    sub.add_parser('plan', help='Read local cards offline')
    s=sub.add_parser('init', help='Preview project config and sample specs; --apply writes missing files')
    s.add_argument('--apply',action='store_true')
    sub.add_parser('skill',help='Print provider-independent AI instructions')
    for name in ('doctor',): add_run_flags(sub.add_parser(name))
    add_run_flags(sub.add_parser('members', help='Read ClickUp List members and authenticated user'))
    s=sub.add_parser('tasks', help='List open tasks in the configured List (read-only); --mine or --assignee to filter')
    who=s.add_mutually_exclusive_group()
    who.add_argument('--mine', action='store_true', help='Your tasks: saved identity, otherwise the ClickUp token owner')
    who.add_argument('--assignee', help='List member ID, email or username')
    s.add_argument('--remember', action='store_true', help='Save the resolved member locally as "mine" (.team-workflow/me.json)')
    s.add_argument('--include-closed', action='store_true')
    add_run_flags(s)
    for name,help_text in (('assign-backlog','Add an assignee to bound plan cards and their direct subtasks'), ('group-backlog','Organize existing delivery units under configured summary parents')):
        s=sub.add_parser(name,help=help_text)
        s.add_argument('--assignee',type=int,required=True); add_run_flags(s,True)
        if name=='group-backlog': s.add_argument('--pr-file',type=Path,help='Verified group PR records to link back to ClickUp')
    for name in ('create', 'read', 'diff', 'sync', 'status', 'estimate', 'update'):
        s = sub.add_parser(name)
        s.add_argument('id')
        add_run_flags(s, name not in ('read', 'diff'))
        if name == 'create': s.add_argument('--priority',choices=['urgent','high','normal','low','none'])
        if name == 'sync':
            s.add_argument('--adopt', action='store_true', help='Preserve legacy text and add a managed spec section')
            s.add_argument('--expect-sha')
        if name == 'status':
            s.add_argument('--to'); s.add_argument('--reason')
        if name == 'estimate':
            group=s.add_mutually_exclusive_group(); group.add_argument('--set'); group.add_argument('--clear', action='store_true')
        if name == 'update':
            s.add_argument('--name'); s.add_argument('--description-file', type=Path)
            s.add_argument('--points',type=int,choices=[1,2,3,5,8])
            s.add_argument('--priority', choices=['urgent','high','normal','low','none'])
            due=s.add_mutually_exclusive_group(); due.add_argument('--due'); due.add_argument('--clear-due', action='store_true')
            start=s.add_mutually_exclusive_group(); start.add_argument('--planned-start'); start.add_argument('--clear-planned-start', action='store_true')
            s.add_argument('--assignee-add', type=int, action='append', default=[])
            s.add_argument('--assignee-remove', type=int, action='append', default=[])
            s.add_argument('--field', action='append', default=[], help='Native custom field NAME=VALUE (validated against live schema)')
    t=sub.add_parser('time'); actions=t.add_subparsers(dest='action', required=True)
    for action in ('list','log','start','stop','running','cancel','edit','delete'):
        s=actions.add_parser(action); s.add_argument('id'); add_run_flags(s, action not in ('list','running'))
        if action=='list': s.add_argument('--from',dest='from_date'); s.add_argument('--until')
        if action=='log':
            s.add_argument('--duration',required=True); s.add_argument('--start'); s.add_argument('--request-id')
        if action in ('edit','delete'):
            s.add_argument('--entry',required=True)
        if action=='edit':
            s.add_argument('--duration');s.add_argument('--start');s.add_argument('--note')
        if action=='delete': s.add_argument('--reason',required=True)
        if action in ('log','start','stop'):
            s.add_argument('--note',default=''); s.add_argument('--assignee',type=int); s.add_argument('--billable',action='store_true')
    s=sub.add_parser('start'); s.add_argument('id'); s.add_argument('--type',choices=['feat','fix','refactor','chore','test'],default='feat'); add_run_flags(s,True)
    s=sub.add_parser('check'); s.add_argument('id'); s.add_argument('--phase',choices=['red','green'],required=True)
    s.add_argument('--label',default='test'); s.add_argument('--reason',default=''); s.add_argument('--timeout',type=int,default=900)
    s.epilog='Append -- followed by the actual test executable and arguments.'
    s=sub.add_parser('approve-red'); s.add_argument('id'); s.add_argument('--reason',required=True); s.add_argument('--apply',action='store_true')
    pr=sub.add_parser('pr'); actions=pr.add_subparsers(dest='action',required=True)
    for action in ('open','read','checks','ready','draft','sync'):
        s=actions.add_parser(action); s.add_argument('id'); add_run_flags(s, action not in ('read','checks'))
        if action=='open': s.add_argument('--draft',action='store_true',default=True)
    s=sub.add_parser('resume'); s.add_argument('receipt',type=Path); s.add_argument('--timeout',type=int,default=600)
    s=sub.add_parser('github',help='Compatibility transport syntax'); s.add_argument('args',nargs=argparse.REMAINDER)
    s=sub.add_parser('local',help='Compatibility API CLI syntax'); s.add_argument('args',nargs=argparse.REMAINDER)
    return p


def write(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')


def _main(argv, r):
    # Preserve the original github/local interface verbatim, including nested --apply.
    words=list(argv)
    skip=0
    while skip<len(words) and words[skip] in ('--root','--via'):
        skip+=2
    if skip<len(words) and words[skip] in ('github','local'):
        return r['legacy_main'](words)
    test_command=[]
    if '--' in words:
        separator=words.index('--'); test_command=words[separator+1:]; words=words[:separator]
    args=parser().parse_args(words)
    if test_command and args.cmd!='check': raise ValueError('Only check accepts an executable after --')
    if args.cmd=='check': args.command=test_command
    root=args.root.resolve()
    if getattr(args,'timeout',1)<=0: raise ValueError('Timeout must be positive')
    if args.cmd=='pr' and getattr(args,'prepare_only',False) and getattr(args,'apply',False):
        raise ValueError('PR preview uses no --apply; prepare-only cannot be combined with PR mutations')
    ops=module('operations'); delivery=module('delivery')
    if args.cmd=='init':
        templates=HERE/'templates'
        targets={'.clickup-workflow.toml':templates/'config.toml', 'Implementation/example-spec.md':templates/'example-spec.md'}
        conflicts=[name for name in targets if (root/name).exists()]
        if conflicts: raise ValueError('Init refuses to overwrite existing files: '+', '.join(conflicts))
        if args.apply:
            for name,source in targets.items():
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(source.read_text())
        value={'apply':args.apply,'root':str(root),'files':list(targets),'next':'Replace example config values and verify statuses with doctor before remote operations.'}
        print(json.dumps(value,indent=2));return value
    if args.cmd=='skill':
        print((HERE/'skill/SKILL.md').read_text());return None
    if args.cmd=='plan': return r['engine']().main(['--root',str(root),'plan'])
    if args.cmd=='resume':
        path=args.receipt.resolve(); receipt=json.loads(path.read_text())
        if receipt['state']=='prepared': raise ValueError('Prepared-only receipt was not pushed')
        return r['finish'](path,receipt,args.timeout)
    cfg=r['tomllib'].loads((root/'.clickup-workflow.toml').read_text())
    def rpc(operation,ident,apply=False,options=None):
        options=options or {}
        if args.via=='local':
            if getattr(args,'prepare_only',False):
                raise PreparedOnly({'prepared_only':True,'operation':operation,'id':ident,'apply':apply,'options':options})
            worker=module('worker')
            value=worker.execute(r['engine'](),cfg,root,operation,ident,apply,options)
            if apply and operation=='pr-metadata' and 'record_metadata' in r:
                r['record_metadata'](root,value,options)
            return value
        path,receipt=r['prepare'](root,operation,ident,apply,getattr(args,'prepare_only',False),options=options)
        if getattr(args,'prepare_only',False):
            raise PreparedOnly({'prepared_only':True,'receipt':str(path),'request':receipt})
        return r['finish'](path,receipt,getattr(args,'timeout',600),emit=False)['result']
    result=None
    if args.cmd=='doctor': result=rpc('doctor',None)
    elif args.cmd=='members': result=rpc('members',None)
    elif args.cmd=='tasks':
        me_path=root/'.team-workflow/me.json'
        if args.remember and not (args.assignee or args.mine): raise ValueError('--remember needs --assignee or --mine')
        options={'include_closed':args.include_closed}
        if args.assignee: options['assignee']=args.assignee
        elif args.mine:
            saved=json.loads(me_path.read_text()) if me_path.exists() else {}
            if saved.get('id'): options.update(assignee=str(saved['id']),assignee_source='saved')
            else: options['mine']=True
        result=rpc('tasks',None,False,options)
        if args.remember and result.get('assignee') and not getattr(args,'prepare_only',False):
            write(me_path,result['assignee'])
    elif args.cmd=='assign-backlog': result=rpc('assign-backlog',None,args.apply,{'assignee':args.assignee})
    elif args.cmd=='group-backlog':
        options={'assignee':args.assignee}
        if args.pr_file: options['prs']=json.loads(args.pr_file.read_text())
        result=rpc('group-backlog',None,args.apply,options)
    elif args.cmd=='create': result=rpc('create-card',args.id,args.apply,{'priority':args.priority} if args.priority is not None else {})
    elif args.cmd=='read': result=rpc('read',args.id)
    elif args.cmd=='update':
        options={}
        for key in ('name','priority','due','planned_start','points'):
            if getattr(args,key) is not None: options[key]=getattr(args,key)
        if args.clear_due: options['due']=None
        if args.clear_planned_start: options['planned_start']=None
        if args.description_file:
            if args.description_file.name=='.env': raise ValueError('Do not upload an environment secret file')
            options['description']=args.description_file.read_text()
        options.update(assignee_add=args.assignee_add,assignee_remove=args.assignee_remove,fields=args.field)
        if not any(key in options for key in ('name','description','priority','due','planned_start','points')) and not args.assignee_add and not args.assignee_remove and not args.field: raise ValueError('Supply at least one field to update')
        for key in ('due','planned_start'):
            if options.get(key): ops.timestamp(options[key])
        result=rpc('update',args.id,args.apply,options)
    elif args.cmd=='status':
        if args.apply and not args.to: raise ValueError('--apply requires --to')
        result=rpc('status',args.id,args.apply,{'to':args.to,'reason':args.reason})
    elif args.cmd=='estimate':
        options={}
        if args.set: options['duration_ms']=ops.duration(args.set)
        elif args.clear: options['duration_ms']=0
        result=rpc('estimate',args.id,args.apply,options)
    elif args.cmd in ('diff','sync'):
        path=root/'.team-workflow/sync-baselines.json'
        baselines=json.loads(path.read_text()) if path.exists() else {}
        options={}
        if args.cmd=='sync':
            options={'adopt':args.adopt,'expected_remote_sha':args.expect_sha or baselines.get(args.id)}
        result=rpc(args.cmd,args.id,getattr(args,'apply',False),options)
        if result.get('remote_sha'):
            baselines[args.id]=result['remote_sha']; write(path,baselines)
    elif args.cmd=='start':
        if cfg.get('delivery',{}).get('pr_unit')=='group':
            raise ValueError('This project uses existing group PR branches; see Implementation/group-prs.json. Do not create a separate branch for a child task.')
        result=delivery.start(r,rpc,cfg,root,args.id,args)
    elif args.cmd=='check': result=delivery.check(r,cfg,root,args.id,args)
    elif args.cmd=='approve-red': result=delivery.approve_red(root,args.id,args)
    elif args.cmd=='pr': result=delivery.pr_command(r,rpc,cfg,root,args.id,args)
    elif args.cmd=='time':
        timer_path=root/'.team-workflow/timer.json'
        timer=json.loads(timer_path.read_text()) if timer_path.exists() else {}
        current_ms=int(time.time()*1000)
        if args.action=='running':
            active=timer.get('active')
            result={'active':active,'elapsed_ms':current_ms-active['start_ms'] if active else 0}
        elif args.action=='cancel':
            active=timer.get('active')
            if not active or active['id']!=args.id: raise ValueError('No matching local stopwatch')
            result={'apply':args.apply,'cancelled_timer':active,'note':'No ClickUp entry is deleted'}
            if args.apply and not args.prepare_only: write(timer_path,{'active':None,'cancelled':active})
        elif args.action in ('edit','delete'):
            options={'entry_id':args.entry}
            if args.action=='delete': options['reason']=args.reason
            else:
                if args.duration: options['duration_ms']=ops.duration(args.duration)
                if args.start: options['start_ms']=ops.timestamp(args.start)
                if args.note is not None: options['note']=args.note
            result=rpc('time-'+args.action,args.id,args.apply,options)
        elif args.action=='list':
            options={key:value for key,value in [('from',args.from_date),('until',args.until)] if value}
            for value in options.values(): ops.timestamp(value)
            result=rpc('time-list',args.id,False,options)
        elif args.action=='start':
            if timer.get('active'): raise ValueError('A local stopwatch is already active; stop it first')
            value={'id':args.id,'start_ms':current_ms,'request_id':str(uuid.uuid4()),'note':args.note,
                   'assignee':args.assignee,'billable':args.billable}
            result={'apply':args.apply,'timer':value,'note':'Local stopwatch only; ClickUp time entry is created on stop --apply'}
            if args.apply and not args.prepare_only: write(timer_path,{'active':value})
        else:
            if args.action=='stop':
                active=timer.get('active')
                if not active or active['id']!=args.id: raise ValueError('No matching active stopwatch')
                options=dict(active)
                end=active.get('end_ms',current_ms)
                options['duration_ms']=end-active['start_ms']
                if args.note: options['note']=args.note
                if args.apply and not args.prepare_only:
                    active.update(end_ms=end,state='pending-stop'); write(timer_path,{'active':active})
            else:
                length=ops.duration(args.duration)
                options={'start_ms':ops.timestamp(args.start) if args.start else current_ms-length,
                         'duration_ms':length,'note':args.note,'assignee':args.assignee,'billable':args.billable,
                         'request_id':args.request_id or str(uuid.uuid4())}
            if options['duration_ms']<=0 or options['start_ms']+options['duration_ms']>current_ms+1000:
                raise ValueError('Actual time must be positive and cannot be in the future')
            result=rpc('time-log',args.id,args.apply,options)
            if args.action=='stop' and args.apply:
                write(timer_path,{'active':None,'last_completed':options})
    if result is not None: print(json.dumps(result,ensure_ascii=False,indent=2))
    return result


class PreparedOnly(Exception):
    def __init__(self,result):
        self.result=result


def main(argv,r):
    try:
        return _main(argv,r)
    except PreparedOnly as prepared:
        print(json.dumps(prepared.result,ensure_ascii=False,indent=2))
        return prepared.result
