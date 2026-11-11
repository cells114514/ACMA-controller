"""Model-free facade over the extracted durable execution queue."""
import copy
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from .config import Config,bind,current,runtime_root
from . import control,vm_tools
from .element_search import COLLECTION,search_snapshot
from .guest_commands import shells


class LazyGuest:
    """Construction and host power operations do not require a guest connection."""
    def __init__(self):self.delegate=None
    def prepare_transport(self):
        if self.delegate is None:
            self.delegate=control.GuestBackend();self.delegate.own_input(True)
        return self.delegate.prepare_transport()
    def own_input(self,owns):
        if self.delegate:self.delegate.own_input(owns)
    def __getattr__(self,name):
        if self.delegate is None:raise RuntimeError('Guest transport is not prepared')
        return getattr(self.delegate,name)


class Controller:
    """One VM, one worker. No model, plan, chat history or goal-completion verdict."""
    _owner=None
    def __init__(self, config=None, backend=None, directory=None):
        self.config=Config.load(config) if config else current()
        if self._owner is not None:raise ValueError('One active Controller per process; reuse it or close it before rebinding')
        self.directory=Path(directory or runtime_root())
        self.lock=threading.RLock()
        self.started=False;self.stopping=threading.Event()
        bind(self.config)
        from . import transport,application_policy
        transport.PRIVATE=self.directory/'connection';transport.ARTIFACTS=self.directory/'artifacts'
        application_policy.POLICY_PATH=self.directory/'queue/application-policy.json'
        self.store=control.Store(self.directory/'queue')
        with self.store.db() as db:
            row=db.execute("SELECT value FROM settings WHERE key='registered_vm_id'").fetchone()
            if row and row[0]!=self.config.vm_id:raise ValueError('Runtime directory belongs to another VM; use a separate data directory')
            db.execute("INSERT OR IGNORE INTO settings VALUES('registered_vm_id',?)",(self.config.vm_id,))
        self.worker=control.Worker(self.store,backend or LazyGuest(),host_control=True)
        self.vm=vm_tools.VMTools(self.directory/'vm-operations')
        self.references={};self.latest_state={};self.thread=None

    def __enter__(self):
        self.start();return self
    def __exit__(self,*args):self.close()

    def start(self):
        if self.started:return
        self.worker.acquire()
        Controller._owner=self
        # Restarts never replay old queued actions or uncertain input.
        with self.store.db() as db:
            db.execute("UPDATE actions SET state='cancelled',finished=? WHERE state='queued'",(control.now(),))
        self.store.set_mode('paused' if not self.unresolved() else 'unknown')
        self.started=True
        def loop():
            try:
                while not self.stopping.is_set():
                    if not self.worker.tick():self.stopping.wait(.05)
            finally:self.worker.close()
        self.thread=threading.Thread(target=loop,daemon=True,name='acma-controller');self.thread.start()

    def close(self):
        if not self.started:return
        self.store.control('pause_requested');self.stopping.set()
        self.thread.join(timeout=2)
        if self.thread.is_alive():
            # Keep ownership until the dispatched operation's worker returns.
            raise RuntimeError('Worker is collecting an in-flight result; do not start another controller')
        self.started=False
        if Controller._owner is self:Controller._owner=None

    def unresolved(self):
        with self.store.db() as db:
            return [dict(r) for r in db.execute("SELECT id,state FROM actions WHERE state IN ('running','unknown')")]

    def control(self,command):
        if command not in {'pause','resume','stop'}:raise ValueError('Unknown control command')
        self.references.clear();self.latest_state.clear()
        self.store.control({'pause':'pause_requested','resume':'resume_requested','stop':'stop_requested'}[command])
        return {'controller':self.store.mode(),'unresolved':self.unresolved()}

    def status(self,identifier=None):
        if identifier:
            try:return self.store.get(identifier)
            except KeyError:return self.vm.status(identifier)
        return {'controller':self.store.mode(),'vm_id':self.config.vm_id,
                'background_shells':shells(self.store),
                'unresolved_actions':self.unresolved(),'pending_vm_operations':self.vm.pending(),
                'recent_actions':self.store.recent(limit=20),
                'recent_vm_operations':[{k:r.get(k) for k in ('operation_id','tool','state','started_at','finished_at')}
                    for r in [self.vm.status(p.name.split('.')[0]) for p in sorted(self.vm.directory.glob('*.request.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:20]]]}

    def resume(self):
        self.control('resume')
        until=time.monotonic()+3
        while self.store.mode()=='resume_requested' and time.monotonic()<until:time.sleep(.02)
        if self.store.mode()!='running':raise ValueError('Controller is not running; inspect unresolved actions')

    def submit(self,spec,task_id=None):
        if not self.started:raise ValueError('Start the controller first')
        if self.vm.pending():raise ValueError('VM operation is unresolved; inspect its receipt, do not send guest actions')
        return self.store.submit(spec,task_id or 'api-'+uuid.uuid4().hex)

    def wait(self,identifier,seconds=120):
        until=time.monotonic()+seconds
        while True:
            row=self.store.get(identifier)
            if row['state'] in control.TERMINAL:return row
            if time.monotonic()>=until:return {**row,'wait_expired':True,'retry_action':False}
            time.sleep(.05)

    def _remember(self,state):
        if not state.get('pid') or not state.get('window_id'):return
        key=(state['pid'],state['window_id'])
        self.references.clear()
        self.latest_state[key]=state
        self.references[key]={e['element_token']:copy.deepcopy(e) for e in state.get('elements',[]) if e.get('element_token')}

    def call(self,name,arguments=None,*,capture=None,seconds=120):
        arguments=copy.deepcopy(arguments or {})
        if not isinstance(arguments,dict):raise ValueError('arguments must be an object')
        if not 0<seconds<=120:raise ValueError('Wait must be 0 < seconds <= 120')
        with self.lock:
            if not self.started:raise ValueError('Start the controller first')
            if name in {'shell_start','shell_status','shell_stop','command_status','command_cancel'} or name=='run_command' and 'command' in arguments:
                from .commands import Commands
                commands=Commands(self)
                if name in {'shell_start','run_command'}:return commands.run(arguments,name=='shell_start',seconds)
                return commands.query(name,arguments,seconds)
            if name=='desktop_mouse' or name in {'press_key','hotkey'}:
                return self._desktop_input(name,arguments,capture,seconds)
            if name=='screenshot_view':
                from .visuals import prepare
                row=self.store.get(arguments['capture']);source=(row.get('result') or {}).get('image_path')
                if row['state']!='succeeded' or not source or not Path(source).resolve().is_relative_to(self.store.directory.resolve()):raise ValueError('A registered controller screenshot is required')
                view=prepare(source,self.store.directory/'views',arguments['capture'],**{k:arguments[k] for k in ('region','max_width','max_height') if k in arguments})
                return {**view,'capture':arguments['capture'],'verification':{'status':'confirmed','reason':'derived read-only view; source freshness not extended'}}
            if name=='execute_batch':
                from .batching import execute
                until=time.monotonic()+min(seconds,30)
                def guard():
                    if self.store.mode()!='running' or self.stopping.is_set() or time.monotonic()>=until:raise ValueError('Batch paused, stopped or deadline reached')
                    if self.unresolved():raise ValueError('A prior action is still running or unknown; inspect its receipt')
                supported={t['name'] for t in self.tools()}
                for item in arguments['actions']:
                    if not isinstance(item,dict) or item.get('tool') not in supported:raise ValueError('Batch contains a tool not supported by this controller')
                def dispatch(tool,args):
                    source=args.pop('capture',None) if tool in {'click','double_click','right_click','drag','scroll','move_cursor'} else None
                    return self.call(tool,args,capture=source,seconds=max(.01,min(seconds,until-time.monotonic())))
                return execute(arguments['actions'],dispatch,guard,seconds=min(seconds,30))
            if arguments.get('coordinate_space')=='view_pixels':
                from .visuals import load,arguments as map_arguments
                view=load(self.store.directory/'views',arguments.get('view_id'))
                mapped=map_arguments({**arguments,'capture':capture or view['source_capture']},view)
                capture=mapped.pop('capture');mapped.pop('coordinate_space');arguments=mapped
            elif 'view_id' in arguments:raise ValueError('view_id requires coordinate_space=view_pixels')
            elif 'coordinate_space' in arguments:
                if arguments.pop('coordinate_space')!='screenshot_pixels':raise ValueError('Use original screenshot_pixels or a registered view_pixels mapping')
            if name in vm_tools.READ|vm_tools.MUTATE:
                if name in vm_tools.MUTATE:
                    if self.store.mode()!='running' or self.unresolved():raise ValueError('Resume the controller and reconcile actions first')
                    self.references.clear();self.latest_state.clear()
                    with self.store.db() as db:
                        db.execute("INSERT OR REPLACE INTO settings VALUES('guest_transport_generation',?)",(uuid.uuid4().hex,))
                return self.vm.call(name,arguments,wait_guard=lambda:self.store.mode()=='running' and not self.stopping.is_set())
            if name=='find_elements':
                if not isinstance(arguments.get('query'),str) or not arguments['query']:raise ValueError('Nonempty query required')
                target={k:arguments[k] for k in ('pid','window_id')}
                row=self.call('get_window_state',{**target,'tree_format':'elements','include_screenshot':True,'full_output':True,**COLLECTION},seconds=seconds)
                if row['state']!='succeeded':return row
                return {'action_id':row['id'],**search_snapshot(row['result'],arguments)}
            if name=='scroll_to_element':
                from .element_scroll import scroll_to_element
                return scroll_to_element(ScrollSession(self),arguments,time.time()+seconds)
            if name=='run_command':
                allowed={'script','timeout_ms','output_limit'}
                if set(arguments)-allowed:raise ValueError('Commands run only in the configured guest workspace')
                spec={'vm_id':self.config.vm_id,'kind':'command','cwd':self.config.guest_workspace,**arguments}
            else:
                if name not in control.READ|control.WRITE or name=='set_value':raise ValueError('Tool is not exposed')
                target=(arguments.get('pid'),arguments.get('window_id'))
                if arguments.get('element_token') and arguments['element_token'] not in self.references.get(target,{}):
                    raise ValueError('Stale or foreign element reference; read this window first')
                spec={'vm_id':self.config.vm_id,'kind':'observe' if name in control.READ else 'desktop','tool':name,'arguments':arguments}
                if capture:spec['capture']=capture
            identifier=self.submit(spec)
            row=self.wait(identifier,seconds)
            result=row.get('result') or {}
            if name=='get_window_state' and row['state']=='succeeded':self._remember(result)
            if name not in control.READ and name!='find_elements':
                self.references.clear()
                # A fresh observation is distinct from proof that the goal succeeded.
                if row['state'] in {'succeeded','unverified'} and arguments.get('pid') and arguments.get('window_id'):
                    read=self.call('get_window_state',{'pid':arguments['pid'],'window_id':arguments['window_id'],'tree_format':'elements','include_screenshot':False},seconds=min(seconds,30))
                    row['post_readback']={'state':read['state'],'action_id':read['id'],'observation':read.get('result'),
                                          'verification':'observation_only_not_goal_completion'}
            return row

    def tools(self):
        from .catalog import definitions
        return definitions()

    def _desktop_input(self,name,args,capture,seconds):
        from .desktop_inputs import key_arguments,validate_native
        if name=='desktop_mouse':
            source=args.pop('capture',capture);space=args.pop('coordinate_space',None)
            if space=='view_pixels':
                from .visuals import load,arguments as map_arguments
                view=load(self.store.directory/'views',args.pop('view_id',None))
                args=map_arguments({**args,'capture':source},view);args.pop('capture');args.pop('coordinate_space');space='screenshot_pixels'
            elif 'view_id' in args:raise ValueError('view_id requires view_pixels')
            if space not in {'screenshot_pixels','normalized_1000'}:raise ValueError('Explicit coordinate space required')
            if space=='normalized_1000':
                row=self.store.get(source);image=row.get('result') or {}
                import struct
                with Path(image['image_path']).open('rb') as f:header=f.read(24)
                if header[:8]!=b'\x89PNG\r\n\x1a\n':raise ValueError('Desktop PNG required')
                width,height=struct.unpack('>II',header[16:24])
                for x,y in ([('from_x','from_y'),('to_x','to_y')] if args.get('action')=='drag' else [('x','y')]):
                    if any(type(args.get(k)) not in {int,float} or not 0<=args[k]<=1000 for k in (x,y)):raise ValueError('Normalized point outside 0..1000')
                    args[x]=args[x]*(width-1)/1000;args[y]=args[y]*(height-1)/1000
            native=args
        else:
            allowed={'pid','window_id'}|({'keys'} if name=='hotkey' else {'key','modifiers'})
            if set(args)-allowed:raise ValueError('Desktop keys accept key/modifiers only; use type_text for ordinary text')
            source=None;native=key_arguments(name,args)
        validate_native(native)
        spec={'vm_id':self.config.vm_id,'kind':'desktop','tool':'desktop_input','arguments':native}
        if source:spec['capture']=source
        identifier=self.submit(spec);row=self.wait(identifier,seconds)
        if row['state'] in {'succeeded','unverified'}:
            read=self.call('get_desktop_state',{'include_screenshot':True},seconds=min(seconds,30))
            row['post_readback']={'state':read['state'],'action_id':read['id'],'observation':read.get('result'),'verification':'observation_only_not_goal_completion'}
        self.references.clear()
        return row


class ScrollSession:
    """Small protocol adapter for the extracted deterministic scrolling routine."""
    def __init__(self,controller):
        self.controller=controller;self.policy=SimpleNamespace(elements=controller.references)
        self.element_source_state=next(reversed(controller.latest_state.values()),{})
        self.dispatcher=SimpleNamespace(mode=controller.store.mode)
        self.current_capture=None;self.window_cache=None
    def _read(self,target,deadline,search_collection=False):
        row=self.controller.call('get_window_state',{**target,'tree_format':'elements','include_screenshot':True,**COLLECTION},seconds=max(.1,min(120,deadline-time.time())))
        if row['state']!='succeeded':raise RuntimeError('Fresh scroll observation unavailable; no gesture repeated')
        state=row['result'];self.current_capture={'capture':row['id'],**target}
        return state
    def _call_once(self,name,args,deadline,scroll_point):
        state=self.controller.latest_state[(args['pid'],args['window_id'])];b=state['window_bounds']
        args={**args,'x':scroll_point['x']+b['x'],'y':scroll_point['y']+b['y']}
        row=self.controller.call(name,args,capture=scroll_point['capture'],seconds=max(.1,min(120,deadline-time.time())))
        if row['state'] not in {'succeeded','unverified'}:raise RuntimeError('Scroll outcome uncertain; inspect receipt, do not repeat')
    def remember_read(self,*args):pass
    def emit(self,*args):pass
