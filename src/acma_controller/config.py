"""Explicit non-secret machine binding; one registered VM per process."""
from dataclasses import dataclass, asdict
import json
import ntpath
import os
from pathlib import Path
import uuid
_BOUND=None


@dataclass(frozen=True)
class Config:
    vm_id: str = '00000000-0000-4000-8000-000000000001'
    vm_name: str = 'ExampleVM'
    guest_user: str = 'operator'
    guest_computer: str = 'WINDOWS-GUEST'
    guest_workspace: str = r'C:\ACMA\Workspace'

    @classmethod
    def load(cls, path):
        value=json.loads(Path(path).read_text(encoding='utf-8-sig'))
        if not isinstance(value,dict) or set(value)!=set(cls.__dataclass_fields__):
            raise ValueError('Configuration fields must match the example exactly')
        value['vm_id']=str(uuid.UUID(value['vm_id'])).upper()
        if any(not isinstance(v,str) or not v.strip() or '\n' in v or '\r' in v or '\0' in v for v in value.values()):
            raise ValueError('Configuration values must be nonempty single-line strings')
        workspace=value['guest_workspace']
        if not ntpath.isabs(workspace) or not ntpath.splitdrive(workspace)[0] or '..' in workspace.replace('/','\\').split('\\'):
            raise ValueError('Guest workspace must be an absolute Windows drive path')
        if value['guest_user'].startswith('-') or any(c in value['guest_user'] for c in '\\/@'):
            raise ValueError('Use a local guest account name, not an SSH target')
        return cls(**value)


def current():
    if _BOUND is not None:return _BOUND
    path=os.environ.get('ACMA_CONTROLLER_CONFIG')
    return Config.load(path) if path else Config()


def runtime_root():
    path=os.environ.get('ACMA_CONTROLLER_HOME')
    if path:return Path(path).expanduser().resolve()
    base=Path(os.environ.get('LOCALAPPDATA',Path.home()/'.local/share'))
    return base/'ACMA-controller'


def bind(config):
    """Existing modules retain one fixed binding; never silently switch machines."""
    global _BOUND
    _BOUND=config
    from . import control,vm_tools,transport
    control.VM_ID=vm_tools.VM_ID=transport.VM_ID=config.vm_id
    control.WORKSPACE=config.guest_workspace
    transport.ALIAS='acma-'+config.vm_id.lower()
