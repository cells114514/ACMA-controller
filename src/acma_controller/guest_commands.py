"""Generic PowerShell and explicit durable background jobs on the fixed guest."""
import json
import math
import ntpath
import re
import time

TERMINAL={'succeeded','failed','timed_out','cancelled','stopped','refused'}


def definitions(legacy):
    def obj(properties,required):return {'type':'object','properties':properties,'required':required,'additionalProperties':False}
    common={'command':{'type':'string','minLength':1,'maxLength':16000},'cwd':{'type':'string'},
            'env':{'type':'object','additionalProperties':{'type':'string'}},'output_limit_bytes':{'type':'integer','minimum':256,'maximum':65536}}
    regular=obj({**common,'timeout_seconds':{'type':'number','minimum':.1,'maximum':120},'wait_seconds':{'type':'number','minimum':0,'maximum':30}},['command'])
    background=obj({**common,'timeout_seconds':{'anyOf':[{'type':'number','minimum':.1,'maximum':2147483},{'type':'null'}]}},['command'])
    status=obj({'shell_id':{'type':'string'},'stdout_offset':{'type':'integer','minimum':0},'stderr_offset':{'type':'integer','minimum':0},'max_bytes':{'type':'integer','minimum':4,'maximum':65536}},['shell_id'])
    return [
        {'name':'run_command','description':'通过SSH在固定客户机执行完整PowerShell命令/多行脚本，默认任务目录及UTF-8，支持env、超时、短等待异步返回；退出码0不等于目标完成，文件/系统任务优先命令并独立核对。保留旧operation配方兼容。','parameters':{'anyOf':[regular,legacy]}},
        {'name':'shell_start','description':'在客户机启动长期后台PowerShell脚本，返回shell_id。默认无运行时限，跨SSH断开和Agent任务到期继续运行；由shell_status读取增量输出、shell_stop请求结束，不重复启动。','parameters':background},
        {'name':'shell_status','description':'凭shell_id只读查询原后台进程及分段输出，offset为UTF-8字节位置，返回next_offsets；不会启动或恢复Shell。','parameters':status},
        {'name':'shell_stop','description':'请求停止指定shell_id及其子进程；只代表停止请求，随后shell_status确认实际退出，不回滚已发生效果。','parameters':obj({'shell_id':{'type':'string'}},['shell_id'])}]


def validate_parameters(args,background=False):
    allowed={'command','cwd','env','output_limit_bytes','timeout_seconds'}| (set() if background else {'wait_seconds'})
    if set(args)-allowed or not isinstance(args.get('command'),str) or not 1<=len(args['command'])<=16000 or '\0' in args['command']:raise ValueError('Full guest PowerShell command required, at most 16000 characters')
    cwd=args.get('cwd')
    if cwd is not None and (not isinstance(cwd,str) or not re.match(r'^[A-Za-z]:[\\/]',cwd) or '\0' in cwd or not ntpath.isabs(cwd)):raise ValueError('cwd must be a guest absolute drive path')
    env=args.get('env',{})
    if not isinstance(env,dict) or len(env)>64 or any(not isinstance(k,str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}',k) or not isinstance(v,str) or '\0' in v or len(v)>4096 for k,v in env.items()) or sum(len(k)+len(v) for k,v in env.items())>16000:raise ValueError('Invalid process-only environment')
    if len({k.casefold() for k in env})!=len(env):raise ValueError('Environment names are case-insensitive on Windows')
    timeout=args.get('timeout_seconds',None if background else 30)
    if timeout is None and not background or timeout is not None and (type(timeout) not in (int,float) or not math.isfinite(timeout) or not .1<=timeout<=(2147483 if background else 120)):raise ValueError('Invalid guest command timeout')
    wait=args.get('wait_seconds',3)
    if not background and (type(wait) not in (int,float) or not math.isfinite(wait) or not 0<=wait<=30):raise ValueError('Invalid wait_seconds')
    limit=args.get('output_limit_bytes',65536 if background else 32768)
    if type(limit) is not int or not 256<=limit<=65536:raise ValueError('Invalid output_limit_bytes')


def shells(store,limit=10):
    with store.db() as db:
        rows=db.execute("SELECT id,task_id,submitted,state,result FROM actions WHERE json_extract(spec,'$.command_v2')=1 AND json_extract(spec,'$.background')=1 ORDER BY submitted DESC LIMIT ?",(limit,)).fetchall()
    return [{'shell_id':r['id'],'origin_task_id':r['task_id'],'created_at':r['submitted'],'start_action_state':r['state'],
        'last_observed_state':json.loads(r['result'] or '{}').get('last_shell_state'),'instruction':'Startup/last observation only; query shell_status for live process state.'} for r in rows]
