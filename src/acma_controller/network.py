"""Discover changing addresses without replacing pinned SSH/guest identities."""
import ipaddress
import json
import os
from pathlib import Path
import subprocess
import uuid

ROOT = Path(__file__).resolve().parent


def windows_inventory():
    env = os.environ.copy()
    powershell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    env = {key: value for key, value in env.items() if key.casefold() != 'psmodulepath'}
    env['PSModulePath'] = str(powershell.parent / 'Modules')
    result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-File',
                             str(ROOT / 'scripts/host_network_inventory.ps1')],
                            capture_output=True, timeout=12, env=env,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode or len(result.stdout) > 65536:
        raise ValueError('Default Switch inventory unavailable; no configuration changed')
    return json.loads(result.stdout.decode('utf-8-sig'))


def candidates(config, inventory):
    host = ipaddress.IPv4Address(inventory['host_address'])
    if not host.is_private or host.is_loopback or host.is_link_local or host.is_unspecified:
        raise ValueError('Invalid Default Switch address')
    network = ipaddress.IPv4Network((str(host), inventory['prefix_length']), strict=False)
    peers = []
    for neighbor in inventory.get('neighbors', []):
        mac = str(neighbor.get('mac', '')).upper().replace(':', '-')
        # Scope is the fixed Hyper-V guest, never scan arbitrary LAN peers.
        if not mac.startswith('00-15-5D-'):
            continue
        try:
            address = ipaddress.IPv4Address(neighbor['address'])
        except (KeyError, ValueError):
            continue
        if address in network and address not in {host, network.network_address, network.broadcast_address}:
            peers.append(str(address))
    previous = ipaddress.IPv4Address(config['address'])
    if previous in network and previous != host:
        peers.insert(0, str(previous))
    peers = list(dict.fromkeys(peers))
    if len(peers) > 8:
        raise ValueError('Too many Hyper-V peers for bounded discovery; no scan or pin change')
    return str(host), peers


def refresh(remote, config, path=None, inventory_reader=None):
    """Read-only health probes; persist addresses only after full original pin checks.

    Never called as retry for a task action. No credentials or host keys are replaced.
    """
    path = Path(path or remote.PRIVATE / 'connection.json')
    before = path.read_bytes()
    if json.loads(before.decode('utf-8-sig')) != config:
        raise ValueError('Connection changed concurrently; reload before discovery')
    try:
        remote.request(config,'health',health_timeout=6)
        return config,{'status':'existing_verified','identity':'original_host_key_and_guest_binding'}
    except (OSError,ValueError,subprocess.SubprocessError):pass
    try:
        inventory = (inventory_reader or windows_inventory)()
    except (OSError, ValueError, subprocess.SubprocessError):
        # If discovery is unavailable, an authenticated old connection can still work.
        remote.request(config, 'health', health_timeout=6)
        return config, {'status': 'existing_verified', 'discovery_available': False}
    host, peers = candidates(config, inventory)
    if not peers:
        raise ValueError('No Default Switch guest neighbor found; guest network startup task may not be installed or ready')
    verified = []
    for address in peers:
        trial = {**config, 'address': address, 'host_address': host}
        try:
            # request uses original strict known_hosts/HostKeyAlias and validates VM ID, computer and SID.
            remote.request(trial, 'health', health_timeout=6)
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        verified.append(trial)
    if len(verified) != 1:
        raise ValueError('Network discovery could not uniquely authenticate the pinned guest; no addresses or keys changed. Check the one-time guest network task installation.')
    result = verified[0]
    if result == config:
        return config, {'status': 'existing_verified', 'discovery_available': True}
    if path.read_bytes() != before:
        raise ValueError('Connection changed during discovery; update refused')
    suffix = uuid.uuid4().hex
    backup = path.with_name('connection.before-network-' + suffix + '.json')
    temporary = path.with_name('connection.network-' + suffix + '.tmp')
    backup.write_bytes(before)
    try:
        temporary.write_text(json.dumps(result, indent=2), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return result, {'status': 'rebound', 'previous_address': config['address'],
                    'address': result['address'], 'host_address': host,
                    'identity': 'original_ssh_host_key_and_guest_vm_computer_sid', 'backup': str(backup)}
