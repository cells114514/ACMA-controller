"""Single-VM durable controller. Standard library only; no host task execution."""
from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import gzip
import importlib.util
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from acma_controller.input_profile import bind as bind_input, infer as infer_input

from .config import current, runtime_root
ROOT = Path(__file__).resolve().parent
VM_ID = current().vm_id
WORKSPACE = current().guest_workspace
READ = {"list_apps", "list_windows", "get_window_state", "get_desktop_state", "get_screen_size", "get_cursor_position", "health_report"}
WRITE = {"bring_to_front", "click", "double_click", "right_click", "type_text", "press_key", "hotkey", "drag", "scroll", "move_cursor", "launch_app"}
TERMINAL = {"succeeded", "failed", "unknown", "cancelled", "unverified", "refused", "timed_out", "stopped"}


def now():
    return datetime.now(timezone.utc).isoformat()


def validate(spec):
    if spec.get("vm_id") != VM_ID:
        raise ValueError("Only the registered guest VM is allowed")
    kind = spec.get("kind")
    if kind == "command":
        if not isinstance(spec.get("script"), str) or not 1 <= len(spec["script"]) <= 16000:
            raise ValueError("Guest PowerShell script required, at most 16000 characters")
        cwd = spec.get("cwd", WORKSPACE).replace("/", "\\").rstrip("\\")
        if cwd.casefold() != WORKSPACE.casefold() or ".." in cwd:
            raise ValueError("P1 commands start only in the dedicated guest workspace")
        timeout, limit = spec.get("timeout_ms", 10000), spec.get("output_limit", 8192)
        if type(timeout) is not int or type(limit) is not int or not 100 <= timeout <= 120000 or not 256 <= limit <= 65536:
            raise ValueError("Invalid command budgets")
    elif kind in {"observe", "desktop"}:
        tool = spec.get("tool")
        if tool not in (READ if kind == "observe" else WRITE):
            raise ValueError("Unknown tool or wrong action kind")
        args = spec.get("arguments", {})
        if not isinstance(args, dict) or any(not isinstance(k, str) or k.startswith("_") for k in args):
            raise ValueError("Invalid tool arguments")
        common = {'session', 'pid', 'window_id', 'scope', 'delivery_mode', 'element_token'}
        fields = {
            'list_apps': set(), 'list_windows': {'app', 'pid'},
            'get_window_state': common | {'include_screenshot', 'include_accessibility_tree', 'tree_format', 'max_depth', 'max_elements', 'timeout_ms', 'since', 'full_output'},
            'get_desktop_state': {'include_screenshot'}, 'get_screen_size': set(), 'get_cursor_position': set(), 'health_report': set(),
            'launch_app': {'name', 'session', 'additional_arguments'}, 'bring_to_front': {'pid', 'window_id'},
            'click': common | {'x', 'y', 'action', 'button', 'modifier'}, 'double_click': common | {'x', 'y', 'modifier'},
            'right_click': common | {'x', 'y'}, 'type_text': common | {'text'}, 'set_value': common | {'value'},
            'press_key': common | {'key', 'modifiers', 'x', 'y'}, 'hotkey': common | {'keys', 'x', 'y'},
            'scroll': common | {'x', 'y', 'direction', 'by', 'amount'},
            'drag': common | {'from_x', 'from_y', 'to_x', 'to_y', 'duration_ms', 'steps', 'modifier', 'button'},
            'move_cursor': {'scope', 'x', 'y', 'cursor_id'},
        }
        if set(args) - fields[tool]:
            raise ValueError('Unsupported arguments: ' + ','.join(sorted(set(args) - fields[tool])))
        if ('x' in args) != ('y' in args):
            raise ValueError('Coordinates must be paired')
        if tool in {'click', 'double_click', 'right_click'} and not args.get('element_token') and not {'x', 'y'} <= args.keys():
            raise ValueError('Click requires an observed element or pixel position')
        if args.get('element_token') and ('x' in args or 'from_x' in args):
            raise ValueError('Do not mix element and pixel targets')
        if tool in {'type_text', 'set_value'}:
            field = 'text' if tool == 'type_text' else 'value'
            if not isinstance(args.get(field), str):
                raise ValueError(field + ' must be a string')
        if tool == 'launch_app':
            from .application_policy import launch_name,check_app
            check_app(launch_name(args.get('name')))
            if 'additional_arguments' in args and (not isinstance(args['additional_arguments'],list) or any(not isinstance(v,str) for v in args['additional_arguments'])):
                raise ValueError('Launch arguments must be an array of strings')
        if tool == 'press_key' and (not isinstance(args.get('key'), str) or not args['key']):
            raise ValueError('Key name required')
        if tool == 'move_cursor' and (args.get('scope') != 'desktop' or not {'x', 'y'} <= args.keys()):
            raise ValueError('Real pointer movement requires desktop scope and observed coordinates')
        if tool == 'hotkey' and (not isinstance(args.get('keys'), list) or not 2 <= len(args['keys']) <= 4
                                 or any(k not in {'ctrl', 'alt', 'shift'} for k in args['keys'][:-1])
                                 or not isinstance(args['keys'][-1], str)):
            raise ValueError('Hotkey requires modifiers then one key')
        if tool == 'scroll' and (args.get('direction') not in {'up', 'down', 'left', 'right'} or args.get('by', 'line') not in {'line', 'page'} or type(args.get('amount', 3)) is not int or not 1 <= args.get('amount', 3) <= 50):
            raise ValueError('Invalid scroll direction or budget')
        if tool == 'drag' and not all(k in args for k in ('from_x', 'from_y', 'to_x', 'to_y')):
            raise ValueError('Drag requires both endpoints')
        if kind == "desktop" and tool != "launch_app":
            if tool != "move_cursor" and (type(args.get("pid")) is not int or type(args.get("window_id")) is not int or min(args["pid"], args["window_id"]) <= 0):
                raise ValueError("Exact positive pid and window_id required")
            if args.get("scope") == "desktop" and tool not in {"move_cursor","drag","click"}:
                raise ValueError("Desktop-wide input is disabled")
            if tool in {'drag','click'} and args.get('scope')=='desktop' and infer_input(spec)['keyboard']:
                raise ValueError('Desktop drag is mouse-only')
            if tool in {'drag','click'} and args.get('scope')=='desktop' and 'delivery_mode' in args:
                raise ValueError('Desktop drag does not select a window delivery mode')
            requested_mode=args.get('delivery_mode','foreground')
            requirements=infer_input(spec)
            if requested_mode!='foreground' and not (requested_mode=='background' and requirements['mouse'] and not requirements['keyboard']):
                raise ValueError('Keyboard input requires foreground; mouse delivery is selected by validated input profile')
        if tool == "drag" and not 1 <= args.get("duration_ms", 500) <= 2000:
            raise ValueError("P1 drag limit is 2 seconds")
        if tool in {"type_text"} and len(args.get("text", args.get("value", ""))) > 4096:
            raise ValueError("Text exceeds P1 action budget")
        coords = {k: args[k] for k in ("x", "y", "from_x", "from_y", "to_x", "to_y") if k in args}
        if coords:
            capture = spec.get("capture")
            if not isinstance(capture, str):
                raise ValueError("Pixel actions require an observed capture action id")
            for v in coords.values():
                if type(v) not in (int, float) or not math.isfinite(v) or v < 0:
                    raise ValueError("Invalid coordinates")
    else:
        raise ValueError("Only guest command, desktop and observe actions are allowed")
    requirements=bind_input(spec)
    if 'surface' in spec and not (spec['surface']=='taskbar' and kind=='desktop' and spec.get('tool')=='drag' and spec.get('arguments',{}).get('scope')=='desktop'):
        raise ValueError('Taskbar surface is only available for a desktop drag')
    delivery=spec.get('delivery')
    if delivery is not None:
        if spec.get('arguments',{}).get('scope')=='desktop' and spec.get('tool') in {'drag','click'}:
            raise ValueError('Desktop drag selects desktop scope, not window delivery modes')
        if (not isinstance(delivery,dict) or set(delivery)!={'mode'} or delivery['mode'] not in {'foreground','background'}
                or kind!='desktop' or not any(requirements.values())):
            raise ValueError('Delivery selection requires a HID action and explicit foreground/background mode')
        if requirements['keyboard'] and delivery['mode']!='foreground':raise ValueError('Keyboard actions cannot request background delivery')
        argument_mode=spec.get('arguments',{}).get('delivery_mode')
        if argument_mode is not None and argument_mode!=delivery['mode']:raise ValueError('Conflicting delivery modes in arguments and execution configuration')
    return spec


class Store:
    def __init__(self, directory=None):
        self.directory = Path(directory or runtime_root()/"queue")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / "state.sqlite3"
        with self.db() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT OR IGNORE INTO settings VALUES('mode','paused');
            CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,
              submitted TEXT NOT NULL,started TEXT,finished TEXT,state TEXT NOT NULL,spec TEXT NOT NULL,result TEXT);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,time TEXT,kind TEXT,detail TEXT);
            CREATE TABLE IF NOT EXISTS command_cancellations(action_id TEXT PRIMARY KEY,requested TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS task_deadlines(task_id TEXT PRIMARY KEY,expires REAL NOT NULL);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def mode(self):
        with self.db() as db:
            return db.execute("SELECT value FROM settings WHERE key='mode'").fetchone()[0]

    def cancel_command(self,action_id,task_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT * FROM actions WHERE id=?',(action_id,)).fetchone()
            if not row or row['task_id']!=task_id or json.loads(row['spec']).get('kind')!='command':raise ValueError('Only this Agent command can be cancelled')
            if row['state']=='queued':
                db.execute("UPDATE actions SET state='cancelled',finished=? WHERE id=?",(now(),action_id))
                return {'action_id':action_id,'state':'cancelled','dispatched':False}
            if row['state']=='running':
                db.execute('INSERT OR IGNORE INTO command_cancellations VALUES(?,?)',(action_id,now()))
                return {'action_id':action_id,'state':'cancel_requested','verification':{'status':'unknown','reason':'wait for command_status; do not resend'}}
            return {'action_id':action_id,'state':row['state'],'cancel_sent':False}

    def command_cancel_requested(self,action_id):
        with self.db() as db:return bool(db.execute('SELECT 1 FROM command_cancellations WHERE action_id=?',(action_id,)).fetchone())

    def control(self, mode):
        if mode not in {"pause_requested", "resume_requested", "stop_requested"}:
            raise ValueError("Invalid control request")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT value FROM settings WHERE key='mode'").fetchone()[0]
            if mode == "resume_requested" and db.execute("SELECT 1 FROM actions WHERE state='unknown' LIMIT 1").fetchone():
                raise ValueError("Unknown action must be reconciled before resume")
            db.execute("UPDATE settings SET value=? WHERE key='mode'", (mode,))
            db.execute("INSERT INTO events(time,kind,detail) VALUES(?,?,?)", (now(), "control", mode))
            if mode == "stop_requested":
                db.execute("UPDATE actions SET state='cancelled',finished=? WHERE state='queued'", (now(),))

    def set_mode(self, mode):
        with self.db() as db:
            db.execute("UPDATE settings SET value=? WHERE key='mode'", (mode,))

    def fail_resume(self, detail):
        """Close write admission after failed resume observations, unless superseded."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE settings SET value='unknown' WHERE key='mode' AND value='resume_requested'").rowcount
            if changed:
                db.execute("INSERT INTO events(time,kind,detail) VALUES(?,?,?)",
                           (now(), "resume_failed", str(detail)[:2000]))
            return bool(changed)

    def submit(self, spec, task_id=None):
        import copy
        spec=copy.deepcopy(spec)
        validate(spec)
        if spec.get("capture"):
            self.check_capture(spec)
        action_id = uuid.uuid4().hex
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            mode = db.execute("SELECT value FROM settings WHERE key='mode'").fetchone()[0]
            deadline=db.execute('SELECT expires FROM task_deadlines WHERE task_id=?',(task_id,)).fetchone()
            if deadline and time.time()>=deadline[0]:raise ValueError('Task countdown ended; no new action admitted')
            if mode != "running" and spec["kind"] != "observe":
                raise ValueError("Input/command admission closed: " + mode)
            db.execute("INSERT INTO actions(id,task_id,submitted,state,spec) VALUES(?,?,?,?,?)",
                       (action_id, task_id or uuid.uuid4().hex, now(), "queued", json.dumps(spec, ensure_ascii=False)))
        return action_id

    def set_task_deadline(self, task_id, expires):
        with self.db() as db:db.execute('INSERT OR REPLACE INTO task_deadlines VALUES(?,?)',(task_id,expires))

    def get(self, action_id):
        with self.db() as db:
            row = db.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if row is None:
            raise KeyError(action_id)
        result = dict(row)
        result["spec"] = json.loads(result["spec"])
        result["result"] = json.loads(result["result"]) if result["result"] else None
        return result

    def recent(self, limit=30, task_id=None):
        with self.db() as db:
            rows = [dict(r) for r in db.execute("SELECT id,task_id,submitted,state,spec FROM actions"+(" WHERE task_id=?" if task_id is not None else "")+" ORDER BY submitted DESC LIMIT ?", ((task_id,limit) if task_id is not None else (limit,)))]
        for row in rows:
            spec = json.loads(row.pop('spec'))
            row['operation'] = spec.get('tool', spec['kind'])
        return rows

    def claim(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE actions SET state='cancelled',finished=?,result=? WHERE state='queued' AND task_id IN (SELECT task_id FROM task_deadlines WHERE expires<=?)",
                       (now(),json.dumps({'dispatched':False,'reason':'Task countdown ended before dispatch'}),time.time()))
            mode = db.execute("SELECT value FROM settings WHERE key='mode'").fetchone()[0]
            rows = db.execute("SELECT id,spec FROM actions WHERE state='queued' ORDER BY submitted").fetchall()
            row = next((r for r in rows if mode == "running" or json.loads(r["spec"])["kind"] == "observe"), None)
            if row:
                db.execute("UPDATE actions SET state='running',started=? WHERE id=?", (now(), row["id"]))
                return row["id"], json.loads(row["spec"])

    def finish(self, action_id, state, result):
        with self.db() as db:
            db.execute("UPDATE actions SET state=?,finished=?,result=? WHERE id=?", (state, now(), json.dumps(result, ensure_ascii=False), action_id))
            if state == "unknown":
                db.execute("UPDATE settings SET value='unknown' WHERE key='mode'")

    def recover(self):
        with self.db() as db:
            ids = [r[0] for r in db.execute("SELECT id FROM actions WHERE state='running'")]
        for action_id in ids:
            self.finish(action_id, "unknown", {"error": "Previous worker ended during dispatch; inspect evidence, no automatic retry"})
        return ids

    def resolve(self, action_id, outcome, evidence):
        if outcome not in {'failed', 'succeeded', 'cancelled'} or not isinstance(evidence, str) or len(evidence.strip()) < 10:
            raise ValueError('Explicit outcome and reconciliation evidence required')
        row = self.get(action_id)
        if row['state'] != 'unknown':
            raise ValueError('Only unknown actions require reconciliation')
        result = row['result'] or {}
        result['resolution'] = {'previous_state': 'unknown', 'outcome': outcome, 'evidence': evidence, 'time': now()}
        self.finish(action_id, outcome, result)
        with self.db() as db:
            db.execute('INSERT INTO events(time,kind,detail) VALUES(?,?,?)', (now(), 'resolution', json.dumps(result['resolution'])))
            if not db.execute("SELECT 1 FROM actions WHERE state='unknown' LIMIT 1").fetchone():
                db.execute("UPDATE settings SET value='paused' WHERE key='mode' AND value='unknown'")

    def pointer_source(self,spec):
        args=spec['arguments'];tool=spec['tool']
        if spec.get('capture'):
            capture=self.check_capture(spec)
            if tool=='move_cursor':return {'space':'screen','points':[[args['x'],args['y']]],'pointer_only':True}
            points=([[args['from_x'],args['from_y']],[args['to_x'],args['to_y']]] if tool=='drag' else [[args['x'],args['y']]])
            if args.get('scope')=='desktop':
                target=self.desktop_drag_target(spec)
                return {'space':'screen','points':points,'expected_bounds':target['window_bounds'],'taskbar':spec.get('surface')=='taskbar'}
            from collections import Counter
            from statistics import median
            mappings=[]
            for element in capture.get('elements',[]):
                f=element.get('frame',{});p=element.get('screenshot_frame',{})
                if all(k in f and k in p for k in ('x','y','w','h')) and min(f['w'],f['h'],p['w'],p['h'])>0:
                    sx,sy=round(f['w']/p['w'],6),round(f['h']/p['h'],6)
                    mappings.append((sx,sy,f['x']-p['x']*sx,f['y']-p['y']*sy))
            if not mappings:raise ValueError('Screenshot-to-screen mapping unavailable; obtain a window capture with element geometry')
            scale=Counter((m[0],m[1]) for m in mappings).most_common(1)[0][0]
            selected=[m for m in mappings if m[:2]==scale]
            ox,oy=median(m[2] for m in selected),median(m[3] for m in selected)
            if any(abs(m[2]-ox)>2 or abs(m[3]-oy)>2 for m in selected):raise ValueError('Inconsistent capture coordinate mapping; observe again')
            screen=[[x*scale[0]+ox,y*scale[1]+oy] for x,y in points]
            return {'space':'screen','points':screen,'expected_bounds':capture.get('window_bounds'),
                    'mapping':{'origin':[ox,oy],'screen_per_pixel':scale,'source':'paired frame and screenshot_frame'}}
        with self.db() as db:
            rows=db.execute("SELECT id,result FROM actions WHERE state='succeeded' AND result IS NOT NULL ORDER BY rowid DESC LIMIT 80").fetchall()
        for row in rows:
            state=json.loads(row['result'])
            if (state.get('pid'),state.get('window_id'))!=(args.get('pid'),args.get('window_id')):continue
            observed=state.get('observed_at')
            if not observed or not -1<=(datetime.now(timezone.utc)-datetime.fromisoformat(observed)).total_seconds()<=30:continue
            token=args.get('element_token')
            elements=[e for e in state.get('elements',[]) if (e.get('element_token')==token if token else e.get('role') in {'Document','Edit'} and 'scroll' in e.get('actions',[]))]
            for e in elements:
                f=e.get('frame',{})
                if f.get('w',0)>0 and f.get('h',0)>0:
                    return {'space':'screen','points':[[f['x']+f['w']/2,f['y']+f['h']/2]],'expected_bounds':state.get('window_bounds'),
                            'source_action':row['id'],'expected_label':e.get('label') if token else None,
                            'expected_element':{'role':e.get('role'),'label':e.get('label'),'frame':dict(f),
                                                'value':e.get('value') if isinstance(e.get('value'),str) and len(e['value'])<=4096 else None} if token else None}
        raise ValueError('Mouse action requires fresh observed target geometry or a capture')

    def desktop_drag_target(self,spec):
        args=spec['arguments']
        with self.db() as db:
            rows=db.execute("SELECT result FROM actions WHERE state='succeeded' AND json_extract(spec,'$.tool')='get_window_state' ORDER BY rowid DESC LIMIT 80").fetchall()
        for row in rows:
            state=json.loads(row['result'] or '{}')
            if (state.get('pid'),state.get('window_id'))!=(args['pid'],args['window_id']):continue
            observed=state.get('observed_at')
            if not observed or not -1 <= (datetime.now(timezone.utc)-datetime.fromisoformat(observed)).total_seconds() <= 30:break
            if state.get('window_bounds'):return state
        raise ValueError('Desktop drag requires a fresh observed exact target window')

    def check_capture(self, spec):
        previous = self.get(spec["capture"])
        result = previous["result"] or {}
        if previous["state"] != "succeeded" or not result.get("capture_id"):
            raise ValueError("Capture is not a successful observed image")
        args = spec["arguments"]
        if spec["tool"] == "move_cursor" or args.get('scope')=='desktop':
            if previous['spec'].get('tool')!='get_desktop_state' or result.get('pid') or result.get('window_id'):raise ValueError('Desktop input requires a desktop capture')
            width, height = result.get("screen_width"), result.get("screen_height")
            if spec['tool'] in {'drag','click'}:self.desktop_drag_target(spec)
        else:
            if result.get("pid") != args.get("pid") or result.get("window_id") != args.get("window_id"):
                raise ValueError("Capture belongs to another window")
            width, height = result.get("screenshot_width"), result.get("screenshot_height")
        if not width or not height:
            raise ValueError("Capture dimensions unavailable")
        for key in ("x", "from_x", "to_x"):
            if key in args and not 0 <= args[key] < width:
                raise ValueError("X outside observed image")
        for key in ("y", "from_y", "to_y"):
            if key in args and not 0 <= args[key] < height:
                raise ValueError("Y outside observed image")
        observed = result.get('observed_at')
        if not observed or not -1 <= (datetime.now(timezone.utc) - datetime.fromisoformat(observed)).total_seconds() <= 30:
            raise ValueError('Capture expired; observe again before pixel input')
        return result


class GuestBackend:
    diagnostic_screenshots=True
    def __init__(self):
        from . import transport
        self.remote = transport
        self.config = self.remote.load_connection()

    def prepare_transport(self):
        from .network import refresh
        self.config = self.remote.load_connection()
        self.config, evidence = refresh(self.remote, self.config)
        return evidence

    def request(self, op, tool=None, arguments=None):
        reply = self.remote.request(self.config, op, tool, arguments)
        if op == 'command_status' and reply.get('command', {}).get('state') in {'succeeded', 'failed', 'stopped', 'timed_out'}:
            result = reply['command']
            if result.get('computer') != self.config['computer'] or result.get('user_sid') != self.config['user_sid']:
                raise ValueError('Command worker identity differs from pinned guest user')
        return reply

    def own_input(self, owns):
        self.remote.CONTROLLER_OWNS_INPUT = owns

    def cua(self, tool, arguments):
        # Applies to GUI, model and direct queue callers; never trust only model scope.
        from .application_policy import load_policy,check_app,launch_name
        try:
            policy=load_policy()
            if tool=='launch_app':check_app(launch_name(arguments.get('name')),policy)
            elif tool in WRITE and tool!='move_cursor' and policy['blocked_apps']:
                observed=self.cua('list_windows',{})
                matches=[w for w in observed.get('windows',[]) if (w.get('pid'),w.get('window_id'))==(arguments.get('pid'),arguments.get('window_id'))]
                if len(matches)!=1 or not matches[0].get('app_name'):raise ValueError('Cannot verify target application against denylist; no input sent')
                check_app(matches[0]['app_name'],policy)
        except (ValueError,OSError) as error:
            return {'effect':'refused','error':str(error),'dispatched':False}
        response = self.request("cua", tool, arguments)
        if tool=='launch_app' and response.get('outcome')=='rejected_before_dispatch' and response.get('dispatched') is False:
            return {'effect':'refused','dispatched':False,'error':response['error'],
                    'refusal':{'code':'launch_preflight_failed','stage':'launch_preflight','reason':response['error']},
                    'response_file':response['response_file']}
        if response["exit_code"]:
            text = response.get("stdout", "") + response.get("stderr", "")
            try:
                refused=json.loads(response.get('stdout',''))
                if refused.get('effect')=='refused' or refused.get('status')=='refused' or refused.get('refusal'):
                    return {**refused,'response_file':response['response_file']}
            except (ValueError,AttributeError):pass
            # Nonzero after dispatch may still have changed the application.
            return {"effect": "unknown", "error": text, "response_file": response["response_file"]}
        try:
            result = json.loads(response["stdout"])
        except json.JSONDecodeError:
            result = {"summary": response["stdout"], "effect": "unverifiable"}
        result["response_file"] = response["response_file"]
        return result

    def pointer_probe(self,args,source):
        payload={**source,'pid':args.get('pid'),'window_id':args.get('window_id')}
        encoded=base64.b64encode(json.dumps(payload).encode('utf-8')).decode('ascii')
        script="$probe=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"+encoded+"'))|ConvertFrom-Json\n"+(ROOT/'scripts/guest_pointer_probe.ps1').read_text(encoding='utf-8')
        # Keep the SSH bridge's single-line JSON request small even as the
        # read-only probe grows; decode the exact trusted source in the guest.
        packed=base64.b64encode(gzip.compress(script.encode('utf-8'),mtime=0)).decode('ascii')
        script=("$m=[IO.MemoryStream]::new([Convert]::FromBase64String('"+packed+"'));"
                "$g=[IO.Compression.GZipStream]::new($m,[IO.Compression.CompressionMode]::Decompress);"
                "$r=[IO.StreamReader]::new($g,[Text.Encoding]::UTF8);"
                "try {& ([ScriptBlock]::Create($r.ReadToEnd()))} finally {$r.Dispose();$g.Dispose();$m.Dispose()}")
        identifier=uuid.uuid4().hex
        self.request('command_start',arguments={'job_id':identifier,'script':script,'cwd':WORKSPACE,'timeout_ms':10000,'output_limit':8192})
        until=time.monotonic()+25
        while time.monotonic()<until:
            response=self.request('command_status',arguments={'job_id':identifier});job=response['command']
            if job['state'] in {'succeeded','failed','stopped','timed_out','unknown'}:
                if job['state']!='succeeded' or job.get('exit_code')!=0 or job.get('output_truncated'):
                    return {'confirmed':False,'reason':'pointer_probe_failed','job_id':identifier,'state':job['state'],'error':job.get('stderr','')[:500]}
                data=json.loads(job['stdout']);data['job_id']=identifier
                from .pointer_match import verify_probe
                verified=verify_probe(data,source,args.get('pid'),args.get('window_id'))
                return self.confirm_pointer_provider(verified,args,source)
            time.sleep(.15)
        # Do not send a mouse action while a probe has uncertain completion.
        return {'confirmed':False,'reason':'pointer_probe_completion_unknown','job_id':identifier}


    def confirm_pointer_provider(self,data,args,source):
        from .pointer_match import coarse_container_only,fresh_provider_match
        if not args.get('element_token') or not coarse_container_only(data,args.get('pid'),args.get('window_id')):return data
        data['provider_confirmation']={'attempted':True,'read_only':True,'input_dispatched':False}
        try:
            fresh=self.cua('get_window_state',{'pid':args['pid'],'window_id':args['window_id'],
                **({'session':args['session']} if args.get('session') else {}),
                'include_screenshot':False,'tree_format':'elements','max_depth':30,'max_elements':500})
            element=fresh_provider_match(source,fresh,args['pid'],args['window_id'])
            if element is None:
                data['provider_confirmation'].update(reason='fresh_element_missing_changed_or_ambiguous',
                    readback_status=fresh.get('status'),readback_refusal=fresh.get('refusal'),
                    readback_error=fresh.get('error'),collection_complete=fresh.get('elements_complete'))
                return data
            # New snapshot invalidates the old token even when the final root check fails.
            root_source={**source,'expected_element':None,'expected_label':None}
            root_check=self.pointer_probe(args,root_source)
            if not root_check.get('confirmed'):
                data['provider_confirmation'].update(reason='fresh_root_or_geometry_check_failed',root_check=root_check)
                return data
            prior=data.get('target_identity') or {};current=root_check.get('target_identity') or {}
            if not prior or any(prior.get(k)!=current.get(k) for k in ('pid','window_id','exe_path','session_id','process_started_at')):
                data['provider_confirmation']['reason']='target_process_identity_changed'
                return data
            old_token=args['element_token'];args['element_token']=element['element_token']
            data.update(confirmed=True,reason='fresh_provider_element_and_native_root_confirmed',target_identity=current)
            data['provider_confirmation'].update(reason='unique_name_role_bounds_and_native_hit',
                original_token=old_token,refreshed_token=element['element_token'],snapshot_id=fresh.get('snapshot_id'),
                element={k:element.get(k) for k in ('role','label','frame','enabled','actions')},root_check=root_check)
        except Exception as error:
            data['provider_confirmation'].update(reason='provider_confirmation_failed',error=str(error)[:400])
        return data

    def window_transition_probe(self,target,candidate_hwnd):
        payload={**target,'candidate_window_id':candidate_hwnd}
        encoded=base64.b64encode(json.dumps(payload).encode()).decode('ascii')
        script="$probe=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('"+encoded+"'))|ConvertFrom-Json\n"+(ROOT/'scripts/guest_window_transition_probe.ps1').read_text(encoding='utf-8')
        packed=base64.b64encode(gzip.compress(script.encode('utf-8'),mtime=0)).decode('ascii')
        script=("$m=[IO.MemoryStream]::new([Convert]::FromBase64String('"+packed+"'));"
                "$g=[IO.Compression.GZipStream]::new($m,[IO.Compression.CompressionMode]::Decompress);"
                "$r=[IO.StreamReader]::new($g,[Text.Encoding]::UTF8);"
                "try {& ([ScriptBlock]::Create($r.ReadToEnd()))} finally {$r.Dispose();$g.Dispose();$m.Dispose()}")
        identifier=uuid.uuid4().hex
        self.request('command_start',arguments={'job_id':identifier,'script':script,'cwd':WORKSPACE,'timeout_ms':8000,'output_limit':8192})
        until=time.monotonic()+15
        while time.monotonic()<until:
            job=self.request('command_status',arguments={'job_id':identifier})['command']
            if job['state'] in {'succeeded','failed','stopped','timed_out','unknown'}:
                if job['state']!='succeeded' or job.get('exit_code')!=0 or job.get('output_truncated'):return {'read_only':True,'error':'native_transition_probe_failed','job_id':identifier}
                return {**json.loads(job['stdout']),'job_id':identifier}
            time.sleep(.15)
        return {'read_only':True,'error':'native_transition_probe_completion_unknown','job_id':identifier}


class Worker:
    def __init__(self, store, backend, host_control=False):
        self.store, self.backend = store, backend
        self.host_control=host_control
        self.guest_generation=None
        self.shutdown = threading.Event()
        self._lock_file = None
        self._mutex = None

    def prepare_guest(self):
        if hasattr(self.backend,'prepare_transport'):
            self.backend.prepare_transport()
        observations=[self.backend.cua('get_screen_size',{}),self.backend.cua('list_apps',{})]
        errors=[o.get('error') for o in observations if o.get('error')]
        if errors:raise ValueError('; '.join(map(str,errors)))

    def acquire(self):
        if __import__("os").name == "nt":
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
            kernel.CreateMutexW.restype = ctypes.c_void_p
            ctypes.set_last_error(0)
            handle = kernel.CreateMutexW(None, True, "Local\\ACMA-P1-input-" + VM_ID)
            if not handle:
                raise OSError("Cannot create VM controller mutex")
            if ctypes.get_last_error() == 183:
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel.CloseHandle(handle)
                raise RuntimeError("Another controller owns this VM input queue")
            self._mutex = handle
            if hasattr(self.backend, 'own_input'):
                self.backend.own_input(True)
            self.store.recover()
            return
        self._lock_file = open(self.store.directory / "worker.lock", "a+b")
        self._lock_file.seek(0)
        self._lock_file.write(b"0")
        self._lock_file.flush()
        self._lock_file.seek(0)
        try:
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._lock_file.close()
            self._lock_file = None
            raise RuntimeError("Another controller owns this VM input queue")
        self.store.recover()
        if hasattr(self.backend, 'own_input'):
            self.backend.own_input(True)

    def close(self):
        if hasattr(self.backend, 'own_input'):
            self.backend.own_input(False)
        if self._mutex:
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel.ReleaseMutex(self._mutex)
            kernel.CloseHandle(self._mutex)
            self._mutex = None
        if self._lock_file:
            self._lock_file.close()
            self._lock_file = None

    def gate(self):
        if self.store.mode() != "running":
            raise InterruptedError("Input admission paused or stopped before next dispatch")

    def prepare_result(self, action_id, result):
        for key in list(result):
            if key.endswith("_b64") and "screenshot" in key:
                image = self.store.directory / (action_id + ".png")
                image.write_bytes(base64.b64decode(result.pop(key), validate=True))
                result["image_path"] = str(image)
        result["observed_at"] = now()
        (self.store.directory / (action_id + ".result.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result

    def execute(self, action_id, spec):
        if spec["kind"] == "command":
            self.gate()
            job_id = action_id
            args = {"job_id": job_id, "script": spec["script"], "cwd": spec.get("cwd", WORKSPACE),
                    "timeout_ms": spec.get("timeout_ms", 10000), "output_limit": spec.get("output_limit", 8192)}
            started = self.backend.request("command_start", arguments=args)
            cancelled = False
            cancel_reply = None
            deadline = time.monotonic() + args["timeout_ms"] / 1000 + 30
            while time.monotonic() < deadline:
                mode = self.store.mode()
                if (mode == "stop_requested" or self.store.command_cancel_requested(action_id)) and not cancelled:
                    cancel_reply = self.backend.request("command_cancel", arguments={"job_id": job_id})
                    cancelled = True
                reply = self.backend.request("command_status", arguments={"job_id": job_id})
                result = reply["command"]
                result["response_file"] = reply["response_file"]
                result['start_response_file'] = started['response_file']
                if cancel_reply:
                    result['stop_request'] = cancel_reply['command']
                    result['stop_response_file'] = cancel_reply['response_file']
                if result["state"] not in {"starting", "running"}:
                    return result["state"], result
                time.sleep(0.25)
            return "unknown", {"job_id": job_id, "error": "Command result deadline elapsed; no repeat execution"}
        args = dict(spec.get("arguments", {}))
        tool = spec["tool"]
        focus = None
        inputs=bind_input(spec)
        pointer=None;windows_before=None
        if spec["kind"] == "desktop":
            self.gate()
            if spec.get("capture"):
                capture = self.store.check_capture(spec)
                if tool != "move_cursor":
                    if args.get('scope')=='desktop':capture=self.store.desktop_drag_target(spec)
                    current = self.backend.cua("list_windows", {})
                    windows = [w for w in current.get("windows", []) if w.get("pid") == args["pid"] and w.get("window_id") == args["window_id"]]
                    if len(windows) != 1 or windows[0].get("bounds") != capture.get("window_bounds"):
                        return "refused", {"error": "Window geometry changed; obtain a fresh capture"}
            if inputs['keyboard']:
                focus = self.backend.cua("bring_to_front", {"pid": args["pid"], "window_id": args["window_id"]})
                if not focus.get("landed_on_target") or int(focus.get("now_fg_hwnd", "0"), 0) != args["window_id"]:
                    return "refused", {"error": "Exact foreground not verified", "focus": focus}
                args["delivery_mode"] = "foreground"
            if inputs['mouse']:
                try:source=self.store.pointer_source(spec)
                except ValueError as error:return 'refused',{'effect':'refused','error':str(error),'input':inputs}
                if not hasattr(self.backend,'pointer_probe'):return 'refused',{'effect':'refused','error':'Pointer validation backend unavailable','input':inputs}
                pointer=self.backend.pointer_probe(args,source)
                pointer['expected_label']=source.get('expected_label')
                if not pointer.get('confirmed'):return 'refused',{'effect':'refused','error':'Pointer hit/occlusion validation failed','input':inputs,'pointer_verification':pointer}
                if args.get('scope')=='desktop' and tool in {'drag','click'}:args.pop('delivery_mode',None)
                elif not inputs['keyboard'] and tool!='move_cursor':args['delivery_mode']=spec.get('delivery',{}).get('mode',args.get('delivery_mode','background'))
            self.gate()
        # 0.38.0 hotkey's XAML branch precedes foreground routing and misclassifies
        # classic Notepad. press_key+modifiers is the supported explicit SendInput route.
        requested_tool = tool
        if spec['kind'] == 'desktop' and tool == 'hotkey':
            keys = args.pop('keys')
            args['key'], args['modifiers'] = keys[-1], keys[:-1]
            tool = 'press_key'
        if spec['kind']=='desktop' and tool in {'click','double_click','right_click'} and hasattr(self.backend,'window_transition_probe'):
            windows_before=self.backend.cua('list_windows',{}).get('windows',[])
        result = self.backend.cua(tool, args)
        if result.get('effect')=='unknown' and windows_before is not None:
            from .window_transition import post_click_window,verify_transition
            candidate=post_click_window(result.get('error'))
            if candidate:
                try:
                    after=self.backend.cua('list_windows',{}).get('windows',[])
                    probe=self.backend.window_transition_probe({'pid':args['pid'],'window_id':args['window_id']},candidate)
                    prior=(pointer or {}).get('target_identity')
                    source_gone=not any((w.get('pid'),w.get('window_id'))==(args['pid'],args['window_id']) for w in after)
                    candidate_state=None
                    if prior and source_gone and probe.get('foreground_hwnd')!=candidate and probe.get('candidate',{}).get('pid'):
                        candidate_state=self.backend.cua('get_window_state',{'pid':probe['candidate']['pid'],
                            'window_id':candidate,'include_screenshot':False,'tree_format':'elements'})
                    transition=verify_transition({'pid':args['pid'],'window_id':args['window_id']},windows_before,after,probe,candidate,
                                                 source_identity=prior,candidate_state=candidate_state)
                    if transition['status']=='verified':
                        from .application_policy import check_app,launch_name
                        check_app(transition['next_target'].get('app_name'))
                        check_app(launch_name(probe['candidate']['exe_path']))
                        result['original_error']=result.pop('error');result['original_effect']=result['effect'];result['effect']='confirmed'
                    result['window_transition']=transition
                except Exception as error:result['window_transition']={'status':'unverified','reason':str(error),'input_replayed':False}

        result['input']=inputs
        result['dispatch']={'tool':tool,'delivery_mode':args.get('delivery_mode'),
                            'scope':args.get('scope','window'),
                            'selection':'explicit' if spec.get('delivery') or 'delivery_mode' in spec.get('arguments',{}) else 'controller_default',
                            'guard':'mouse_hit_and_keyboard_focus' if inputs['mouse'] and inputs['keyboard'] else 'mouse_hit' if inputs['mouse'] else 'keyboard_focus' if inputs['keyboard'] else 'no_hid_input'}
        if pointer:result['pointer_verification']=pointer
        if focus:
            result['focus_verification'] = {'landed_on_target': focus.get('landed_on_target'),
                                             'now_fg_hwnd': focus.get('now_fg_hwnd'), 'response_file': focus.get('response_file')}
        if requested_tool != tool:
            result['requested_tool'], result['executed_tool'] = requested_tool, tool
        if spec["kind"] == "observe":
            return ("failed" if result.get("error") else "succeeded"), result
        effect = result.get("effect")
        if result.get("refusal") or effect == "refused" or result.get("status") == "refused":
            return "refused", result
        if result.get("error") or effect == "unknown":
            return "unknown", result
        if effect == "confirmed" or (tool == "bring_to_front" and result.get("landed_on_target")):
            return "succeeded", result
        return "unverified", result

    def tick(self):
        mode = self.store.mode()
        if mode in {"pause_requested", "stop_requested"}:
            self.store.set_mode("paused" if mode == "pause_requested" else "stopped")
        elif mode == "resume_requested":
            # Resume is acknowledged only after fresh guest observations.
            try:
                if self.host_control:
                    # Host lifecycle admission must work while the guest is Off.
                    # The first queued guest operation gets its own handshake.
                    self.guest_generation=None
                elif hasattr(self.backend, 'prepare_transport'):
                    evidence = self.backend.prepare_transport()
                    with self.store.db() as db:
                        db.execute('INSERT INTO events(time,kind,detail) VALUES(?,?,?)',
                                   (now(), 'transport_ready', json.dumps(evidence)))
                observations = [] if self.host_control else [self.backend.cua("get_screen_size", {}), self.backend.cua("list_apps", {})]
                errors = [o.get("error") for o in observations if o.get("error")]
                if errors:
                    self.store.fail_resume("; ".join(str(error) for error in errors))
                    return False
            except Exception as error:
                self.store.fail_resume(str(error))
                return False
            if self.store.mode() == "resume_requested":
                self.store.set_mode("running")
        item = self.store.claim()
        if item:
            action_id, spec = item
            try:
                if self.host_control:
                    with self.store.db() as db:
                        row=db.execute("SELECT value FROM settings WHERE key='guest_transport_generation'").fetchone()
                    generation=row[0] if row else 'initial'
                    if self.guest_generation!=generation:
                        try:self.prepare_guest()
                        except Exception as error:
                            self.store.finish(action_id,'refused',{'effect':'refused','dispatched':False,
                                'error':'客户机连接尚未就绪；宿主VM工具仍可使用：'+str(error),
                                'retry_action':False,'refusal':{'code':'guest_unavailable_before_dispatch'}})
                            return True
                        self.guest_generation=generation
                    if self.store.mode()!='running' and spec['kind']!='observe':
                        self.store.finish(action_id,'cancelled',{'dispatched':False,'reason':'Control changed during guest handshake'})
                        return True
                state, result = self.execute(action_id, spec)
                if getattr(self.backend,'diagnostic_screenshots',False) and spec.get('kind')=='desktop' and result.get('window_transition'):
                    try:
                        snapshot=self.prepare_result(action_id+'-diagnostic',self.backend.cua('get_desktop_state',{}))
                        result['diagnostic_snapshot']={'image_path':snapshot.get('image_path'),'observed_at':snapshot['observed_at'],'read_only':True,
                            'available':bool(snapshot.get('image_path')),'error':snapshot.get('error')}
                    except Exception as error:result['diagnostic_snapshot']={'available':False,'error':str(error),'read_only':True}
                self.store.finish(action_id, state, self.prepare_result(action_id, result))
            except InterruptedError as error:
                self.store.finish(action_id, "cancelled", {"error": str(error), "no_retry": True})
            except Exception as error:
                self.store.finish(action_id, "unknown", {"error": str(error), "no_retry": True})
        return bool(item)

    def run(self):
        self.acquire()
        try:
            if hasattr(self.backend, 'prepare_transport'):
                try:
                    evidence = self.backend.prepare_transport()
                    kind = 'transport_ready'
                except Exception as error:
                    evidence = {'error': str(error), 'task_action_retried': False}
                    kind = 'transport_unavailable'
                    if self.store.mode() in {'running', 'resume_requested'}:
                        self.store.set_mode('paused')
                with self.store.db() as db:
                    db.execute('INSERT INTO events(time,kind,detail) VALUES(?,?,?)',
                               (now(), kind, json.dumps(evidence)))
            while not self.shutdown.is_set():
                if not self.tick():
                    self.shutdown.wait(0.15)
        finally:
            self.close()
