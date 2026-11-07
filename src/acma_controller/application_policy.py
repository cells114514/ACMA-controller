"""User-owned guest application denylist. Never editable through model tools."""
import json
import ntpath
import re
import threading
from pathlib import Path

from .config import runtime_root
POLICY_PATH=runtime_root()/'queue/application-policy.json'
_LOCK=threading.RLock()


def app_name(value):
    if not isinstance(value,str) or not value.strip() or len(value)>260:raise ValueError('Application executable name required')
    value=value.strip()
    if ntpath.basename(value)!=value or re.search(r'[<>:"/\\|?*\x00-\x1f]',value) or value in {'.','..'}:
        raise ValueError('Use executable names such as QQ.exe, without paths or wildcards')
    return (value if value.lower().endswith('.exe') else value+'.exe').casefold()


def load_policy(path=None):
    path=Path(path or POLICY_PATH)
    with _LOCK:
        if not path.exists():return {'mode':'denylist','blocked_apps':[]}
        value=json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(value,dict) or set(value)!={'mode','blocked_apps'} or value['mode']!='denylist' or not isinstance(value['blocked_apps'],list) or len(value['blocked_apps'])>128:
            raise ValueError('Invalid application denylist configuration; input stopped')
        return {'mode':'denylist','blocked_apps':sorted(set(app_name(a) for a in value['blocked_apps']))}


def save_policy(names,path=None):
    path=Path(path or POLICY_PATH)
    if not isinstance(names,list) or len(names)>128:raise ValueError('At most 128 blocked executable names')
    value={'mode':'denylist','blocked_apps':sorted(set(app_name(a) for a in names))}
    with _LOCK:
        path.parent.mkdir(parents=True,exist_ok=True);temporary=path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8');temporary.replace(path)
    return value


def check_app(name,policy=None):
    name=app_name(name)
    if name in (policy or load_policy())['blocked_apps']:raise ValueError('Application is blocked by user denylist: '+name)
    return name


def launch_name(name):
    # No URL, AUMID, document association or inline command: one executable only.
    if not isinstance(name,str) or not name.lower().endswith('.exe') or any(c in name for c in '"\r\n\x00'):
        raise ValueError('Launch requires an executable name or absolute guest EXE path')
    base=ntpath.basename(name)
    app_name(base)
    if name!=base and (not ntpath.isabs(name) or not re.match(r'^[A-Za-z]:[\\/]',name) or '..' in name.replace('/','\\').split('\\')):
        raise ValueError('Launch path must be an absolute local guest EXE path')
    return base.casefold()
