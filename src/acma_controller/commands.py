"""Model-independent command resources. One queue, immutable startup ownership."""
import json
import time
from .guest_commands import validate_parameters, TERMINAL


def receipt(store,row):
    value=row.get('result') or {};state=row['state']
    result={'id':row['id'],'action_id':row['id'],'state':state,'origin_task_id':row['task_id'],
        'target':{'vm_id':row['spec']['vm_id'],'cwd':value.get('cwd',row['spec'].get('cwd'))},
        'stdout':value.get('stdout',''),'stderr':value.get('stderr',''),'exit_code':value.get('exit_code'),
        'dispatched':value.get('dispatched',False if state in {'queued','refused','cancelled'} else None),
        'process_started':value.get('process_started'),'exit_confirmed':bool(value.get('exit_confirmed')),
        'tree_terminated':bool(value.get('tree_terminated')),'timed_out':bool(value.get('timed_out')),
        'stdout_truncated':bool(value.get('stdout_truncated',value.get('output_truncated',False))),
        'stderr_truncated':bool(value.get('stderr_truncated',value.get('output_truncated',False))),
        'error':value.get('error'),'result':value,'result_json':None,
        'verification':{'status':'confirmed' if state=='succeeded' else 'waiting' if state in {'queued','running'} else 'unknown' if state=='unknown' else 'mismatch'}}
    if result['stdout'] and not result['stdout_truncated'] and not result['stderr_truncated']:
        try:result['result_json']=json.loads(result['stdout'])
        except ValueError:pass
    return result


class Commands:
    def __init__(self,controller):self.c=controller;self.store=controller.store

    def run(self,args,background,seconds):
        validate_parameters(args,background)
        c=self.c;timeout=args.get('timeout_seconds',None if background else 30)
        spec={'vm_id':c.config.vm_id,'kind':'command','command_v2':True,'background':background,
            'script':args['command'],'cwd':args.get('cwd',c.config.guest_workspace),'env':args.get('env',{}),
            'timeout_ms':0 if timeout is None else int(timeout*1000),'output_limit':args.get('output_limit_bytes',65536 if background else 32768)}
        signature=json.dumps(spec,sort_keys=True)
        with self.store.db() as db:
            rows=db.execute("SELECT id,state,spec,result FROM actions WHERE json_extract(spec,'$.command_v2')=1").fetchall()
        for row in rows:
            stored=json.loads(row['spec'])
            if json.dumps({k:stored.get(k) for k in spec},sort_keys=True)!=signature:continue
            live=(json.loads(row['result'] or '{}').get('last_shell_state') not in TERMINAL) if background else row['state'] in {'queued','running','unknown'}
            if live and row['state'] not in {'failed','refused','cancelled','timed_out','stopped'}:raise ValueError('Same command resource is unresolved; query its original ID, do not restart')
        identifier=c.submit(spec)
        row=c.wait(identifier,seconds if background else max(.001,min(seconds,args.get('wait_seconds',3))))
        result=receipt(self.store,row)
        if background:
            result.update(shell_id=identifier,persistent=True,shell_state=(row.get('result') or {}).get('state','starting'))
            if row['state']=='succeeded':result['verification']={'status':'waiting'}
        return result

    def original(self,identifier,background):
        import re
        if not isinstance(identifier,str) or not re.fullmatch('[0-9a-f]{32}',identifier):raise ValueError('Registered command ID required')
        row=self.store.get(identifier)
        if not row['spec'].get('command_v2') or bool(row['spec'].get('background'))!=background:raise ValueError('Wrong command resource kind')
        return row

    def query(self,name,args,seconds):
        background=name.startswith('shell_');identifier=args.get('shell_id' if background else 'action_id')
        original=self.original(identifier,background)
        stopping=name in {'shell_stop','command_cancel'}
        permitted={'shell_id','stdout_offset','stderr_offset','max_bytes'} if background and not stopping else {'shell_id'} if background else {'action_id'}
        if set(args)-permitted:raise ValueError('Unexpected command resource arguments')
        if background and original['state'] not in {'succeeded','unknown'}:return {**receipt(self.store,original),'shell_id':identifier}
        if not background and original['state']!='unknown':
            if name=='command_cancel':return self.store.cancel_command(identifier,original['task_id'])
            return receipt(self.store,original)
        spec={'vm_id':self.c.config.vm_id,'kind':'shell_control' if stopping else 'observe','tool':'shell_stop' if stopping else 'shell_status',
            'arguments':{'shell_id':identifier,'resource_kind':'shell' if background else 'command',**{k:v for k,v in args.items() if k in {'stdout_offset','stderr_offset','max_bytes'}}}}
        action=self.c.submit(spec);row=self.c.wait(action,seconds);value=row.get('result') or {}
        if not background and not stopping and row['state']=='succeeded' and value.get('shell_state') in TERMINAL and value.get('exit_confirmed'):
            final={k:v for k,v in value.items() if k!='shell_state'};self.store.finish(identifier,value['shell_state'],final)
            return receipt(self.store,self.store.get(identifier))
        if not background:
            if stopping:return {'action_id':identifier,'state':'cancel_requested' if row['state']=='succeeded' else 'unknown','stop_request':value,'verification':{'status':'waiting'}}
            if row['state']=='succeeded':return receipt(self.store,{**original,'state':value.get('shell_state','unknown'),'result':{k:v for k,v in value.items() if k!='shell_state'}})
            return {**receipt(self.store,original),'status_action_id':action,'status_error':value,'verification':{'status':'unknown'}}
        return {**value,'state':row['state'],'shell_id':identifier,'query_action_id':action,'origin_task_id':original['task_id'],
            'verification':{'status':'unknown' if row['state']!='succeeded' or value.get('shell_state')=='unknown' else 'waiting' if stopping and value.get('shell_state') not in TERMINAL else 'confirmed'}}
