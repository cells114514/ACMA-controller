# Run locally inside the guest. Does not install Driver/OpenSSH or change login.
#Requires -RunAsAdministrator
param(
    [Parameter(Mandatory)][guid]$VMId,
    [Parameter(Mandatory)][string]$Driver,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{64}$')][string]$DriverSha256,
    [string]$Workspace='C:\ACMA\Workspace',
    [string]$Destination='C:\Program Files\ACMA-controller\bridge'
)
$ErrorActionPreference='Stop'
$identity=[Security.Principal.WindowsIdentity]::GetCurrent()
if(Test-Path -LiteralPath (Join-Path $Destination 'binding.json')){throw 'A binding already exists. Inspect it; this helper does not overwrite registrations.'}
if(-not [IO.Path]::IsPathRooted($Workspace) -or $Workspace -notmatch '^[A-Za-z]:[\\/]' -or '..' -in ($Workspace -split '[\\/]')){throw 'Use an absolute local workspace'}
if((Get-FileHash -LiteralPath $Driver -Algorithm SHA256).Hash -ine $DriverSha256){throw 'Driver hash mismatch'}
$null=New-Item -ItemType Directory -Path $Destination -Force
$acl=[Security.AccessControl.DirectorySecurity]::new()
$admins=[Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
$system=[Security.Principal.SecurityIdentifier]::new('S-1-5-18')
$acl.SetOwner($admins);$acl.SetAccessRuleProtection($true,$false)
foreach($sid in @($admins,$system)){
 $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
}
$acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($identity.User,'ReadAndExecute','ContainerInherit,ObjectInherit','None','Allow'))
Set-Acl -LiteralPath $Destination -AclObject $acl
foreach($name in @('guest_cua_ssh_entry.ps1','command-worker.ps1','command-runner.cs')){
 Copy-Item -LiteralPath (Join-Path $PSScriptRoot $name) -Destination (Join-Path $Destination $name)
}
$null=New-Item -ItemType Directory -Path $Workspace -Force
$config=@{vm_id=$VMId.ToString().ToUpperInvariant();computer=$env:COMPUTERNAME;user_sid=$identity.User.Value;
          driver=[IO.Path]::GetFullPath($Driver);driver_sha256=$DriverSha256.ToLowerInvariant();guest_workspace=[IO.Path]::GetFullPath($Workspace).TrimEnd('\')}
$config|ConvertTo-Json|Set-Content -LiteralPath (Join-Path $Destination 'binding.json') -Encoding UTF8
$config|ConvertTo-Json
Write-Output 'Bridge files prepared. Configure a dedicated forced-command SSH key and an interactive Driver daemon as documented; no credentials or services were changed.'
