param([Parameter(Mandatory=$true)][string]$JobId)
$ErrorActionPreference='Stop'
$utf8=[Text.UTF8Encoding]::new($false)
$config=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'binding.json') -Raw -Encoding UTF8|ConvertFrom-Json
if($JobId -notmatch '^[0-9a-f]{32}$'){throw 'Invalid job id'}
$dir=Join-Path 'C:\ProgramData\ACMA-P1\jobs' $JobId
$result=@{job_id=$JobId;state='failed';computer=$env:COMPUTERNAME;user=[Security.Principal.WindowsIdentity]::GetCurrent().Name;user_sid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value;started_at=[DateTimeOffset]::Now.ToString('o')}
$result.is_admin=([Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
$result.phase='preparing'
$result.exit_confirmed=$false
try {
    $request=[IO.File]::ReadAllText((Join-Path $dir 'request.json'),$utf8)|ConvertFrom-Json
    $cwd=[IO.Path]::GetFullPath([string]$request.cwd)
    $workspace=[string]$config.guest_workspace
    if($cwd -ine $workspace -and -not $cwd.StartsWith($workspace+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Command cwd outside guest workspace'}
    [IO.Directory]::CreateDirectory($cwd)|Out-Null
    $result.cwd=$cwd
    Add-Type -Path (Join-Path $PSScriptRoot 'command-runner.cs')
    $signal=Join-Path $dir 'start.flag'
    $script=@"
`$ErrorActionPreference='Stop'
`$ProgressPreference='SilentlyContinue'
`$encoding=[Text.UTF8Encoding]::new(`$false)
[Console]::OutputEncoding=`$encoding
`$OutputEncoding=`$encoding
`$deadline=[DateTime]::UtcNow.AddSeconds(15)
while(-not [IO.File]::Exists('$signal')){if([DateTime]::UtcNow -gt `$deadline){exit 252};Start-Sleep -Milliseconds 50}
Set-Location -LiteralPath '$($cwd.Replace("'","''"))'
`$global:LASTEXITCODE=0
try {
& {
$($request.script)
}
if(-not `$?){exit 1}
exit `$LASTEXITCODE
}catch{[Console]::Error.WriteLine((`$_|Out-String));exit 1}
"@
    $parseErrors=$null
    [System.Management.Automation.Language.Parser]::ParseInput($script,[ref]$null,[ref]$parseErrors)|Out-Null
    if($parseErrors){throw ('Command wrapper parse failed: '+($parseErrors|Out-String))}
    $encoded=[Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
    $result.phase='dispatching'
    $run=[AcmaCommandRunner]::Run((Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'),$encoded,$cwd,$dir,[int]$request.timeout_ms,[int]$request.output_limit)
    foreach($name in @('stdout','stderr','exit_code','timed_out','stopped','output_truncated','child_pid','tree_terminated')){$result[$name]=$run.$name}
    $result.state=if($run.stopped){'stopped'}elseif($run.timed_out){'timed_out'}elseif($run.exit_code -eq 0){'succeeded'}else{'failed'}
    $result.exit_confirmed=$true
    $result.phase='finished'
}catch{if($result.phase -eq 'dispatching'){$result.state='unknown'};$result.error=($_|Out-String)}
finally {
    $result.finished_at=[DateTimeOffset]::Now.ToString('o')
    $temp=Join-Path $dir 'result.tmp'
    [IO.File]::WriteAllText($temp,($result|ConvertTo-Json -Depth 8 -Compress),$utf8)
    [IO.File]::Move($temp,(Join-Path $dir 'result.json'))
}
