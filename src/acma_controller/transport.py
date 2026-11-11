"""Pinned SSH transport to the registered ACMA guest; never a local task runner."""
from __future__ import annotations

import argparse
import base64
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import uuid
from datetime import datetime, timezone

from .config import current, runtime_root
ROOT = Path(__file__).resolve().parent
PRIVATE = runtime_root() / "connection"
ARTIFACTS = runtime_root() / "artifacts"
VM_ID = current().vm_id
ALIAS = "acma-" + VM_ID.lower()
CONTROLLER_OWNS_INPUT = False


def check_input_owner(operation, tool):
    """Legacy probes may read, but may not compete with the unified worker."""
    readonly = {"list_apps", "list_windows", "get_window_state", "get_desktop_state", "get_screen_size", "get_cursor_position", "health_report", "get_accessibility_tree", "verify_state", "list_sessions", "get_session"}
    mutates = operation in {"command_start", "command_cancel", "configure_driver"} or (operation == "cua" and tool not in readonly)
    if not mutates or CONTROLLER_OWNS_INPUT or sys.platform != "win32":
        return
    import ctypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenMutexW.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.OpenMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenMutexW(0x00100000, False, "Local\\ACMA-P1-input-" + VM_ID)
    if handle:
        kernel.CloseHandle(handle)
        raise ValueError("Unified controller owns this VM; submit through its queue instead of a legacy probe")


def load_connection(path: Path | None = None) -> dict:
    path=Path(path or PRIVATE/'connection.json')
    config = json.loads(path.read_text(encoding="utf-8-sig"))
    if config.get("status") != "pinned":
        raise ValueError("Guest bootstrap report and SSH host-key pin are required")
    if config.get("vm_id") != VM_ID or config.get("user") != current().guest_user:
        raise ValueError("Unregistered VM/user")
    if config.get("computer") != current().guest_computer or not config.get("user_sid", "").startswith("S-1-5-21-"):
        raise ValueError("Guest identity not pinned")
    address = ipaddress.ip_address(config["address"])
    if address.version != 4 or not address.is_private or address.is_loopback or address.is_link_local or address.is_unspecified or address.is_multicast:
        raise ValueError("Guest must have a private, nonlocal IPv4 address")
    if str(address) == config.get("host_address"):
        raise ValueError("Host task execution refused")
    for key in ("key", "known_hosts", "ssh_config"):
        candidate = Path(config[key]).resolve()
        if not candidate.is_relative_to(PRIVATE.resolve()) or not candidate.is_file():
            raise ValueError(f"Missing or unregistered local SSH material: {key}")
    known = Path(config["known_hosts"]).read_text(encoding="utf-8").strip()
    if known != f"{ALIAS} {config['host_key']}":
        raise ValueError("SSH host-key pin does not match connection binding")
    return config


def validate_reply(config: dict, request_id: str, reply: dict) -> None:
    if reply.get("request_id") != request_id:
        raise ValueError("Mismatched response request ID")
    if reply.get("error"):
        raise ValueError("Guest bridge refused or failed: " + str(reply["error"]))
    for key in ("vm_id", "computer", "user_sid"):
        if reply.get(key) != config[key]:
            raise ValueError("Guest response identity mismatch: " + key)


def is_launch_preflight_refusal(config,request_id,reply):
    return (isinstance(reply,dict) and all(reply.get(k)==v for k,v in
        [('request_id',request_id),*[(k,config[k]) for k in ('vm_id','computer','user_sid')]])
        and reply.get('launch_failure_protocol')=='preflight-v1' and reply.get('phase')=='launch_preflight'
        and reply.get('code')=='launch_preflight_failed' and reply.get('dispatched') is False
        and reply.get('outcome')=='rejected_before_dispatch' and isinstance(reply.get('error'),str))


def is_command_preflight_refusal(config,request_id,reply):
    return (isinstance(reply,dict) and reply.get('request_id')==request_id
        and all(reply.get(k)==config.get(k) for k in ('vm_id','computer','user_sid'))
        and reply.get('command_failure_protocol')=='v2' and reply.get('phase')=='command_preflight'
        and reply.get('code')=='command_preflight_failed' and reply.get('dispatched') is False)


def ssh_argv(config: dict) -> list[str]:
    return [
        "ssh.exe", "-F", config["ssh_config"], "-T", "-i", config["key"],
        "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
        "-o", "ForwardAgent=no", "-o", "StrictHostKeyChecking=yes",
        "-o", "ConnectionAttempts=1", "-o", "ConnectTimeout=5",
        "-o", f"HostKeyAlias={ALIAS}",
        # OpenSSH parses this option as a list of paths after argv decoding.
        # Preserve a username containing spaces as one path in that parser too.
        "-o", f'UserKnownHostsFile="{Path(config["known_hosts"]).as_posix()}"',
        "-l", current().guest_user, config["address"],
    ]


def request(config: dict, operation: str, tool: str | None = None, arguments: dict | None = None,
            *, health_timeout: float | None = None) -> dict:
    if operation not in ("health", "cua", "read_test_file", "configure_driver", "command_start", "command_status", "command_cancel"):
        raise ValueError("No local shell or arbitrary transport operation")
    if health_timeout is not None and (operation != 'health' or not 1 <= health_timeout <= 10):
        raise ValueError('Short discovery timeout is only permitted for read-only health')
    check_input_owner(operation, tool)
    if operation == "cua" and (not isinstance(arguments, dict) or not tool or not tool.replace("_", "").isalpha()):
        raise ValueError("Cua tool and object arguments required")
    request_id = uuid.uuid4().hex
    payload = {"request_id": request_id, "vm_id": VM_ID, "op": operation}
    if operation == "cua":
        payload.update(tool=tool, arguments=arguments)
        # Trusted controller configuration, never a model-supplied tool argument.
        from acma_controller.application_policy import load_policy
        payload['application_policy']=load_policy()
    elif operation == "read_test_file":
        if not isinstance(arguments, dict) or not isinstance(arguments.get("path"), str):
            raise ValueError("Guest test-file path required")
        payload["path"] = arguments["path"]
    elif operation.startswith("command_") and operation!="command_capabilities":
        if not isinstance(arguments, dict):
            raise ValueError("Command arguments required")
        payload["arguments"] = arguments
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    audit = {"request_id": request_id, "vm_id": VM_ID, "operation": operation, "tool": tool,
             "started_at": datetime.now(timezone.utc).isoformat(), "state": "dispatching"}
    audit_path = ARTIFACTS / f"{request_id}.audit.json"
    audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        result = subprocess.run(ssh_argv(config), input=json.dumps(payload, ensure_ascii=False) + "\n",
                                capture_output=True, text=True, encoding="utf-8", errors="strict",
                                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                                timeout=health_timeout if health_timeout is not None else 120 if operation == "configure_driver" else 40)
        if len(result.stdout) > 17_000_000:
            raise ValueError("Oversize response; reconcile before another action")
        if result.returncode:
            try:failure=json.loads(result.stdout)
            except (ValueError,TypeError):failure=None
            if (operation=='cua' and tool=='launch_app' and is_launch_preflight_refusal(config,request_id,failure)) or (operation=='command_start' and is_command_preflight_refusal(config,request_id,failure)):
                response_path=ARTIFACTS/f"{request_id}.response.json"
                response_path.write_text(json.dumps(failure,ensure_ascii=False,indent=2),encoding='utf-8')
                audit.update(state='rejected_before_dispatch',response_file=str(response_path),error_code=failure['code'])
                return {**failure,'response_file':str(response_path)}
            raise ValueError(f"SSH/guest exit {result.returncode}: {result.stderr[:1000]} {result.stdout[:1000]}")
        reply = json.loads(result.stdout)
        validate_reply(config, request_id, reply)
        response_path = ARTIFACTS / f"{request_id}.response.json"
        response_path.write_text(json.dumps(reply, ensure_ascii=False, indent=2), encoding="utf-8")
        # This is transport completion, never desktop-action acceptance.
        audit.update(state="response_received", guest_exit_code=reply.get("exit_code"), response_file=str(response_path))
        return {**reply, "request_id": request_id, "response_file": str(response_path)}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        audit.update(state="failed_or_unknown_no_retry", error=str(exc))
        raise
    finally:
        audit["finished_at"] = datetime.now(timezone.utc).isoformat()
        audit_path.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
