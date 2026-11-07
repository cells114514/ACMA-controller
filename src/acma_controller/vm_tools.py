"""Fixed Hyper-V host adapter; never sends lifecycle commands over guest SSH.

Persist before launch. A missing/late receipt blocks subsequent mutations; status
reads never repeat an operation. Host processes are hidden and not killed on timeout.
"""
import json
import os
from pathlib import Path
import subprocess
import threading
import time
import uuid

from .control import ROOT, VM_ID, now
from .config import current, runtime_root

READ = {'vm_status', 'vm_list_checkpoints', 'vm_operation_status'}
MUTATE = {'vm_start', 'vm_shutdown', 'vm_restart', 'vm_save', 'vm_resume',
          'vm_create_checkpoint', 'vm_restore_checkpoint'}
LOCK = threading.Lock()


def definitions():
    def obj(properties):
        return {'type':'object','properties':properties,'required':list(properties),'additionalProperties':False}
    text = {'type':'string','minLength':1,'maxLength':120}
    descriptions = {
        'vm_status':'只读查询固定ACMA虚拟机电源、运行状态、Hyper-V报告的IP。Running不代表桌面或网络就绪。',
        'vm_list_checkpoints':'只读列出ACMA检查点及精确ID。检查点不是模型对话checkpoint。',
        'vm_operation_status':'按operation_id等待先前生命周期操作回执，最多20秒；收到终态立即返回，暂停/到期提前返回。等待由控制器本地执行，不调用模型或Hyper-V、不重复派发。running/unknown不得重试原动作。',
        'vm_start':'启动处于Off状态的ACMA。异步返回operation_id，随后查vm_operation_status；不等待桌面登录。',
        'vm_shutdown':'正常关闭ACMA，不强制断电。异步回执；系统更新时禁止调用。',
        'vm_restart':'正常关闭后仅在确认Off时启动一次。不会强制重启；系统更新时禁止调用。异步查回执。',
        'vm_save':'保存ACMA运行状态到磁盘并暂停运行（Hyper-V Saved）。不是建立检查点；系统更新时禁止调用。',
        'vm_resume':'恢复处于Saved状态的ACMA。恢复后旧窗口/截图引用不可用，需重新观察。',
        'vm_create_checkpoint':'为ACMA建立具名检查点；类型沿用VM配置，不修改认证或快照策略。系统更新时禁止调用。异步查回执。',
        'vm_restore_checkpoint':'按列举得到的精确checkpoint_id恢复ACMA，可能丢失检查点之后的数据。仅用户明确要求恢复时调用，且VM必须已Off；不自动关机、不自动启动。',
    }
    return [{'name':name,'description':description+' 通过固定宿主Hyper-V适配器执行，不是客户机Shell；宿主权限不足会明确拒绝。',
             'parameters':obj({'operation_id':text} if name=='vm_operation_status' else
                              {'name':text} if name=='vm_create_checkpoint' else
                              {'checkpoint_id':{'type':'string','pattern':'^[0-9a-fA-F-]{36}$'}} if name=='vm_restore_checkpoint' else {})}
            for name,description in descriptions.items()]


class VMTools:
    def __init__(self, directory=None):
        self.directory=Path(directory or runtime_root()/'vm-operations')
        self.directory.mkdir(parents=True,exist_ok=True)

    def command(self, request, output):
        ps=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        return [str(ps),'-NoProfile','-NonInteractive','-File',str(ROOT/'scripts/host_vm_operation.ps1'),
                '-RequestPath',str(request),'-ResultPath',str(output)]

    def paths(self, identifier):
        if not isinstance(identifier,str) or len(identifier)!=32 or any(c not in '0123456789abcdef' for c in identifier):
            raise ValueError('Invalid VM operation ID')
        return self.directory/(identifier+'.request.json'),self.directory/(identifier+'.result.json')

    def status(self, identifier):
        request,output=self.paths(identifier)
        if not request.is_file():raise ValueError('Unknown VM operation ID')
        original=json.loads(request.read_text(encoding='utf-8'))
        if output.is_file():return json.loads(output.read_text(encoding='utf-8-sig'))
        return {'operation_id':identifier,'vm_id':VM_ID,'tool':original['tool'],
                'state':'running' if original.get('launched') else 'unknown',
                'started_at':original['started_at'],'retry_action':False,
                'instruction':'只查询本operation_id；无终态回执时禁止重复派发，进程退出也不证明操作未发生。'}

    def wait_status(self, identifier, wait_guard=None, seconds=20):
        started=time.monotonic()
        deadline=started+seconds
        while True:
            result=self.status(identifier)
            if result['state']!='running':break
            if wait_guard and not wait_guard():
                result={**result,'wait_interrupted':True}
                break
            remaining=deadline-time.monotonic()
            if remaining<=0:break
            time.sleep(min(.25,remaining))
        return {**result,'waited_seconds':round(time.monotonic()-started,3),
                'wait_limit_seconds':seconds,'wait_source':'controller_local_receipt',
                'instruction':'终态已返回时按证据继续；仍running仅说明本次等待结束，后续可继续查询同一编号，不重新执行原动作。'}

    def call(self, name, arguments, wait_guard=None):
        if name=='vm_operation_status':return self.wait_status(arguments['operation_id'],wait_guard)
        if name not in READ|MUTATE:raise ValueError('Unknown VM tool')
        if name=='vm_restore_checkpoint':
            if str(uuid.UUID(arguments['checkpoint_id'])).lower()!=arguments['checkpoint_id'].lower():raise ValueError('Invalid checkpoint ID')
        if name=='vm_create_checkpoint' and (not arguments['name'].strip() or len(arguments['name'])>120):raise ValueError('Invalid checkpoint name')
        with LOCK:
            # The service worker is singleton. Disk receipts survive service restart.
            if name in MUTATE:
                for path in self.directory.glob('*.request.json'):
                    previous=json.loads(path.read_text(encoding='utf-8'))
                    if previous['tool'] in MUTATE and self.status(previous['operation_id'])['state'] in {'running','unknown'}:
                        return {'state':'refused','dispatched':False,'reason':'先前VM操作尚无确定终态，先查询其回执；不会重复派发',
                                'operation_id':previous['operation_id']}
            identifier=uuid.uuid4().hex
            request,output=self.paths(identifier)
            payload={'operation_id':identifier,'vm_id':VM_ID,'vm_name':current().vm_name,'tool':name,'arguments':arguments,'started_at':now(),'launched':False}
            temporary=request.with_suffix('.tmp')
            temporary.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
            os.replace(temporary,request)
            try:
                process=subprocess.Popen(self.command(request,output),stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                         creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            except OSError as error:
                result={'operation_id':identifier,'vm_id':VM_ID,'state':'refused','dispatched':False,'error':str(error)}
                output.write_text(json.dumps(result),encoding='utf-8');return result
            payload.update(launched=True,pid=process.pid)
            temporary=request.with_suffix('.tmp')
            temporary.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
            os.replace(temporary,request)
        if name in READ:
            try:process.wait(timeout=12)
            except subprocess.TimeoutExpired:pass
        return self.status(identifier)

    def pending(self):
        return [self.status(p.stem.split('.')[0]) for p in self.directory.glob('*.request.json')
                if json.loads(p.read_text(encoding='utf-8'))['tool'] in MUTATE
                and self.status(p.stem.split('.')[0])['state'] in {'running','unknown'}]
