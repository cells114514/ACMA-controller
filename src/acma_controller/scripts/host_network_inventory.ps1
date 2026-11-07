# Read-only discovery; no VM lifecycle, elevation, network scan or guest commands.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$addresses = @(Get-NetIPAddress -InterfaceAlias 'vEthernet (Default Switch)' -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' })
if ($addresses.Count -ne 1) { throw 'Default Switch must have one current IPv4 address.' }
$address = $addresses[0]
$neighbors = @(Get-NetNeighbor -InterfaceIndex $address.InterfaceIndex -AddressFamily IPv4 |
    Where-Object { [string]$_.State -in @('Reachable', 'Stale', 'Delay', 'Probe', 'Permanent') } |
    ForEach-Object { @{address=$_.IPAddress; mac=$_.LinkLayerAddress} })
@{host_address=$address.IPAddress; prefix_length=$address.PrefixLength; neighbors=$neighbors} |
    ConvertTo-Json -Depth 4 -Compress
