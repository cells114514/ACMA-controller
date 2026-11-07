# Forced SSH entry. Desktop tools still pass through the canonical Cua daemon.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$utf8 = [Text.UTF8Encoding]::new($false)
[Console]::InputEncoding = $utf8
[Console]::OutputEncoding = $utf8
$OutputEncoding = $utf8
$env:DO_NOT_TRACK = '1'
$env:CUA_TELEMETRY = '0'
$requestId = $null
$launchPreflight = $false

function Invoke-AcmaDriverCli {
    param([string]$Driver, [string]$NativeArguments, [string]$InputJson)
    $encoding=[Text.UTF8Encoding]::new($false)
    $info=[Diagnostics.ProcessStartInfo]::new()
    $info.FileName=$Driver;$info.Arguments=$NativeArguments
    $info.UseShellExecute=$false;$info.CreateNoWindow=$true
    $info.RedirectStandardInput=$true;$info.RedirectStandardOutput=$true;$info.RedirectStandardError=$true
    $info.StandardOutputEncoding=$encoding;$info.StandardErrorEncoding=$encoding
    $process=[Diagnostics.Process]::new();$process.StartInfo=$info
    if(-not $process.Start()){throw 'Could not start fixed guest Driver CLI'}
    try{
        $outTask=$process.StandardOutput.ReadToEndAsync();$errTask=$process.StandardError.ReadToEndAsync()
        if($InputJson){$process.StandardInput.WriteLine($InputJson)}
        $process.StandardInput.Close()
        if(-not $process.WaitForExit(30000)){
            try{$process.Kill()}catch{}
            throw 'Guest CLI timeout; action outcome unknown, no retry'
        }
        return @{exit_code=$process.ExitCode;stdout=$outTask.GetAwaiter().GetResult();stderr=$errTask.GetAwaiter().GetResult()}
    }finally{$process.Dispose()}
}

function Get-AcmaIntegrityRid {
    param([int]$ProcessId)
    if(-not ('AcmaTokenRead' -as [type])){
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class AcmaTokenRead {
 [DllImport("kernel32.dll")] static extern IntPtr OpenProcess(uint access, bool inherit, int pid);
 [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
 [DllImport("advapi32.dll")] static extern bool OpenProcessToken(IntPtr process, uint access, out IntPtr token);
 [DllImport("advapi32.dll")] static extern bool GetTokenInformation(IntPtr token, int kind, IntPtr data, int length, out int needed);
 [DllImport("advapi32.dll")] static extern IntPtr GetSidSubAuthorityCount(IntPtr sid);
 [DllImport("advapi32.dll")] static extern IntPtr GetSidSubAuthority(IntPtr sid, uint index);
 public static int ReadRid(int pid) {
  IntPtr process=OpenProcess(0x1000,false,pid),token=IntPtr.Zero,data=IntPtr.Zero;
  if(process==IntPtr.Zero)return -1;
  try {
   if(!OpenProcessToken(process,8,out token))return -1;
   int needed;GetTokenInformation(token,25,IntPtr.Zero,0,out needed);
   if(needed<=0)return -1;
   data=Marshal.AllocHGlobal(needed);
   if(!GetTokenInformation(token,25,data,needed,out needed))return -1;
   IntPtr sid=Marshal.ReadIntPtr(data);int count=Marshal.ReadByte(GetSidSubAuthorityCount(sid));
   if(count<=0)return -1;
   return Marshal.ReadInt32(GetSidSubAuthority(sid,(uint)(count-1)));
  } finally {if(data!=IntPtr.Zero)Marshal.FreeHGlobal(data);if(token!=IntPtr.Zero)CloseHandle(token);CloseHandle(process);}
 }
}
'@
    }
    return [AcmaTokenRead]::ReadRid($ProcessId)
}

try {
    $config = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'binding.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if ($env:COMPUTERNAME -cne $config.computer -or $identity.User.Value -cne $config.user_sid) { throw 'Guest identity mismatch' }
    $line = [Console]::ReadLine()
    if (-not $line -or $line.Length -gt 65536) { throw 'Invalid request size' }
    $request = $line | ConvertFrom-Json
    $requestId = [string]$request.request_id
    if ($requestId -notmatch '^[0-9a-f]{32}$' -or $request.vm_id -cne $config.vm_id) { throw 'Invalid request identity' }
    $driver = [string]$config.driver
    if ((Get-FileHash -LiteralPath $driver -Algorithm SHA256).Hash.ToLowerInvariant() -cne $config.driver_sha256) { throw 'Driver digest mismatch' }
    if($request.op -eq 'configure_driver'){
        $principal=[Security.Principal.WindowsPrincipal]::new($identity)
        if(-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'SSH token is not elevated; interactive administrator setup required'}
        $setupJournal=Join-Path $PSScriptRoot 'focus-service-result.json'
        if(Test-Path -LiteralPath $setupJournal){throw 'Driver configuration journal exists; reconcile it before another lifecycle action'}
        if(@(Get-Process -Name cua-driver -ErrorAction SilentlyContinue).Count){throw 'A Driver process already exists; no automatic stop or duplicate launch'}
        $task=Get-ScheduledTask -TaskName 'cua-driver-serve' -ErrorAction SilentlyContinue
        if($task){
            $taskUser=[string]$task.Principal.UserId
            $taskSid=if($taskUser -like 'S-1-*'){$taskUser}else{([Security.Principal.NTAccount]::new($taskUser)).Translate([Security.Principal.SecurityIdentifier]).Value}
            if($taskSid -cne $config.user_sid -or $task.State -eq 'Running'){throw 'Existing Cua task owner/state is not suitable for one-shot setup'}
            Export-ScheduledTask -TaskName 'cua-driver-serve' | Set-Content -LiteralPath (Join-Path $PSScriptRoot 'focus-task.before.xml') -Encoding UTF8
        }
        $setupState=@{request_id=$requestId;started_at=[DateTimeOffset]::Now.ToString('o');completed=$false;phase='before_registration';start_requested=$false}
        $setupState | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $setupJournal -Encoding UTF8
        try{
            $result=Invoke-AcmaDriverCli $driver 'telemetry disable'
            if($result.exit_code -ne 0){throw ('Telemetry setup failed: '+$result.stderr)}
            $result=Invoke-AcmaDriverCli $driver 'autostart enable'
            if($result.exit_code -ne 0){throw ('Task registration failed: '+$result.stderr)}
            $task=Get-ScheduledTask -TaskName 'cua-driver-serve'
            $taskUser=[string]$task.Principal.UserId
            $taskSid=if($taskUser -like 'S-1-*'){$taskUser}else{([Security.Principal.NTAccount]::new($taskUser)).Translate([Security.Principal.SecurityIdentifier]).Value}
            if($taskSid -cne $config.user_sid -or [string]$task.Principal.RunLevel -ne 'Highest' -or [string]$task.Principal.LogonType -ne 'Interactive' -or ($task.Actions.Arguments -join ' ') -notlike ('*'+$driver+'*')){throw 'Registered task does not match expected user, path, or interactive Highest profile'}
            $setupState.phase='registered';$setupState.start_requested=$true
            $setupState | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $setupJournal -Encoding UTF8
            $result=Invoke-AcmaDriverCli $driver 'autostart kick'
            if($result.exit_code -ne 0){throw ('Task start result uncertain; no retry: '+$result.stderr)}
            $timer=[Diagnostics.Stopwatch]::StartNew()
            $daemon=@()
            do{
                $daemon=@(Get-Process -Name cua-driver -ErrorAction SilentlyContinue | Where-Object {$_.Path -ieq $driver -and $_.SessionId -gt 0})
                if($daemon.Count){break}
                Start-Sleep -Milliseconds 250
            }while($timer.Elapsed.TotalSeconds -lt 12)
            if($daemon.Count -ne 1){throw 'Expected one interactive Driver process; no new start attempted'}
            $rid=Get-AcmaIntegrityRid $daemon[0].Id
            if($rid -ne 12288){throw ('Driver integrity not verified as High; observed RID '+$rid)}
            $setupState.phase='interactive_high_process_verified'
            $setupState.daemon=@{pid=$daemon[0].Id;session_id=$daemon[0].SessionId;path=$daemon[0].Path;integrity_rid=$rid}
            $setupState.completed=$true
        }catch{$setupState.error=$_.Exception.Message;throw}finally{
            $setupState.finished_at=[DateTimeOffset]::Now.ToString('o')
            $setupState | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $setupJournal -Encoding UTF8
        }
        $reply=@{request_id=$requestId;vm_id=$config.vm_id;computer=$env:COMPUTERNAME;user_sid=$identity.User.Value;bridge_session=[Diagnostics.Process]::GetCurrentProcess().SessionId;exit_code=0;configuration=$setupState}
        [Console]::WriteLine(($reply | ConvertTo-Json -Depth 12 -Compress))
        exit 0
    }
    if($request.op -in @('command_start','command_status','command_cancel')){
        $commandArgs=$request.arguments
        $jobId=[string]$commandArgs.job_id
        if($jobId -notmatch '^[0-9a-f]{32}$'){throw 'Invalid command job id'}
        $jobRoot='C:\ProgramData\ACMA-P1\jobs'
        $jobDir=Join-Path $jobRoot $jobId
        if($request.op -eq 'command_start'){
            if(Test-Path -LiteralPath $jobDir){throw 'Command id already exists; no duplicate execution'}
            if([string]::IsNullOrWhiteSpace([string]$commandArgs.script) -or ([string]$commandArgs.script).Length -gt 16000){throw 'Invalid guest command script'}
            if($commandArgs.timeout_ms -lt 100 -or $commandArgs.timeout_ms -gt 120000 -or $commandArgs.output_limit -lt 256 -or $commandArgs.output_limit -gt 65536){throw 'Invalid guest command budgets'}
            $workspace=[string]$config.guest_workspace
            $cwd=[IO.Path]::GetFullPath([string]$commandArgs.cwd)
            if($cwd -ine $workspace -and -not $cwd.StartsWith($workspace+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Command cwd outside guest workspace'}
            [IO.Directory]::CreateDirectory($jobDir)|Out-Null
            $jobAcl=[Security.AccessControl.DirectorySecurity]::new()
            $jobAcl.SetAccessRuleProtection($true,$false)
            $jobAcl.SetOwner($identity.User)
            foreach($sid in @($identity.User.Value,'S-1-5-18','S-1-5-32-544')){
                $rule=[Security.AccessControl.FileSystemAccessRule]::new([Security.Principal.SecurityIdentifier]::new($sid),'FullControl','ContainerInherit,ObjectInherit','None','Allow')
                $jobAcl.AddAccessRule($rule)
            }
            Set-Acl -LiteralPath $jobDir -AclObject $jobAcl
            [IO.File]::WriteAllText((Join-Path $jobDir 'request.json'),($commandArgs|ConvertTo-Json -Depth 6 -Compress),$utf8)
            $worker=Join-Path $PSScriptRoot 'command-worker.ps1'
            $workerArgs='-WindowStyle Hidden -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File "'+$worker+'" -JobId '+$jobId
            $taskName='ACMA-P1-command-'+$jobId
            $taskAction=New-ScheduledTaskAction -Execute (Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe') -Argument $workerArgs
            $taskPrincipal=New-ScheduledTaskPrincipal -UserId $identity.User.Value -LogonType Interactive -RunLevel Limited
            $taskSettings=New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 4) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
            Register-ScheduledTask -TaskName $taskName -Action $taskAction -Principal $taskPrincipal -Settings $taskSettings -ErrorAction Stop|Out-Null
            $job=@{job_id=$jobId;state='starting';task_name=$taskName;requested_utc=[DateTime]::UtcNow.ToString('o')}
            [IO.File]::WriteAllText((Join-Path $jobDir 'worker.json'),($job|ConvertTo-Json -Compress),$utf8)
            Start-ScheduledTask -TaskName $taskName
        }else{
            if(-not (Test-Path -LiteralPath (Join-Path $jobDir 'request.json'))){throw 'Unknown command id'}
            if($request.op -eq 'command_cancel'){
                [IO.File]::WriteAllText((Join-Path $jobDir 'cancel.flag'),'stop requested',$utf8)
                $job=@{job_id=$jobId;state='stop_requested';exit_confirmed=$false}
            }elseif(Test-Path -LiteralPath (Join-Path $jobDir 'result.json')){
                $job=[IO.File]::ReadAllText((Join-Path $jobDir 'result.json'),$utf8)|ConvertFrom-Json
            }else{
                $workerState=[IO.File]::ReadAllText((Join-Path $jobDir 'worker.json'),$utf8)|ConvertFrom-Json
                if($workerState.task_name){
                    $task=Get-ScheduledTask -TaskName $workerState.task_name -ErrorAction SilentlyContinue
                    $taskInfo=Get-ScheduledTaskInfo -TaskName $workerState.task_name -ErrorAction SilentlyContinue
                    $recent=($workerState.requested_utc -and ([DateTime]::UtcNow-[DateTime]::Parse($workerState.requested_utc)).TotalSeconds -lt 15)
                    $job=@{job_id=$jobId;state=$(if($task.State -eq 'Running'){'running'}elseif($recent -or $taskInfo.LastRunTime.Year -lt 2000){'starting'}else{'unknown'});exit_confirmed=$false;task_result=$taskInfo.LastTaskResult}
                }else{
                    $job=@{job_id=$jobId;state='unknown';exit_confirmed=$false;reason='legacy detached worker returned no result; no retry'}
                }
                if(Test-Path -LiteralPath (Join-Path $jobDir 'result.json')){$job=[IO.File]::ReadAllText((Join-Path $jobDir 'result.json'),$utf8)|ConvertFrom-Json}
            }
        }
        if(Test-Path -LiteralPath (Join-Path $jobDir 'result.json')){
            $taskName='ACMA-P1-command-'+$jobId
            $finishedTask=Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
            $expectedArgs='-WindowStyle Hidden -NoProfile -NonInteractive -ExecutionPolicy RemoteSigned -File "'+(Join-Path $PSScriptRoot 'command-worker.ps1')+'" -JobId '+$jobId
            $knownArgs=@($expectedArgs,$expectedArgs.Replace('-WindowStyle Hidden ',''))
            if($finishedTask -and $finishedTask.State -ne 'Running' -and @($finishedTask.Actions).Count -eq 1 -and $finishedTask.Actions[0].Arguments -cin $knownArgs){
                Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
            }
        }
        $reply=@{request_id=$requestId;vm_id=$config.vm_id;computer=$env:COMPUTERNAME;user_sid=$identity.User.Value;bridge_session=[Diagnostics.Process]::GetCurrentProcess().SessionId;exit_code=0;command=$job}
        [Console]::WriteLine(($reply|ConvertTo-Json -Depth 10 -Compress))
        exit 0
    }
    if ($request.op -eq 'read_test_file') {
        $desktop = [IO.Path]::GetFullPath((Join-Path $env:USERPROFILE 'Desktop'))
        $path = [IO.Path]::GetFullPath([string]$request.path)
        if ([IO.Path]::GetDirectoryName($path) -ine $desktop -or [IO.Path]::GetFileName($path) -notmatch '^ACMA-Cua-P1-[0-9a-f]{32}\.txt$') { throw 'Only ACMA guest desktop test files may be read' }
        foreach ($itemPath in @($desktop,$path)) {
            $item=Get-Item -LiteralPath $itemPath -ErrorAction Stop
            if (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'Reparse targets refused' }
        }
        if ($item.PSIsContainer -or $item.Length -gt 1048576) { throw 'Test file size/type refused' }
        $reply=@{request_id=$requestId;vm_id=$config.vm_id;computer=$env:COMPUTERNAME;user_sid=$identity.User.Value;bridge_session=[Diagnostics.Process]::GetCurrentProcess().SessionId;path=$path;content=[IO.File]::ReadAllText($path,$utf8);sha256=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant();exit_code=0}
        [Console]::WriteLine(($reply | ConvertTo-Json -Depth 5 -Compress))
        exit 0
    }
    $tool = $null
    if ($request.op -eq 'health') {
        $arguments = 'status'
        $inputJson = $null
    } elseif ($request.op -eq 'cua') {
        $tool = [string]$request.tool
        $readOnly = @('list_apps','list_windows','get_window_state','get_desktop_state','get_screen_size','get_cursor_position','health_report','get_accessibility_tree','verify_state','list_sessions','get_session')
        $lifecycle = @('start_session','end_session')
        $write = @('bring_to_front','click','double_click','right_click','type_text','set_value','press_key','hotkey','scroll','move_cursor','drag')
        if ($tool -notin ($readOnly + $lifecycle + $write + @('launch_app'))) { throw 'Tool not allowed by ACMA bridge' }
        if ($null -eq $request.arguments) { throw 'Arguments object required' }
        $argsObject = $request.arguments
        if (@($argsObject.PSObject.Properties | Where-Object Name -like '_*').Count) { throw 'Reserved arguments refused' }
        # The host owns this denylist; it is not a driver/model tool argument.
        $blockedApps=@()
        if($request.application_policy){
            if($request.application_policy.mode -cne 'denylist'){throw 'Invalid application policy mode'}
            $blockedApps=@($request.application_policy.blocked_apps)
            if($blockedApps.Count -gt 128){throw 'Oversize application denylist'}
            foreach($blocked in $blockedApps){
                if($blocked -isnot [string] -or $blocked.Length -gt 260 -or $blocked -match '[<>:"/\\|?*\x00-\x1f]' -or $blocked -notmatch '\.exe$'){throw 'Invalid blocked executable name'}
            }
        }
        if($tool -eq 'launch_app'){
            $launchPreflight=$true
            if(@($argsObject.PSObject.Properties|Where-Object {$_.Name -notin @('name','session','additional_arguments')}).Count){throw 'Unexpected launch arguments'}
            $launch=[string]$argsObject.name
            if($launch -notmatch '\.exe$' -or $launch -match '["\r\n\x00<>|?*]'){throw 'Launch requires one executable'}
            if($launch -match '[\\/]'){
                if($launch -notmatch '^[A-Za-z]:[\\/]' -or @($launch -split '[\\/]'|Where-Object {$_ -eq '..'}).Count){throw 'Absolute local EXE path required'}
                $resolved=[IO.Path]::GetFullPath($launch)
            }else{
                $commands=@(Get-Command -Name $launch -CommandType Application -ErrorAction Stop)
                if($commands.Count -ne 1){throw 'Executable name is ambiguous; use a guest absolute path'}
                $resolved=$commands[0].Source
            }
            if(-not (Test-Path -LiteralPath $resolved -PathType Leaf) -or [IO.Path]::GetExtension($resolved) -ine '.exe'){throw 'Executable not found'}
            if([IO.Path]::GetFileName($resolved) -iin $blockedApps){throw 'Launch blocked by user application denylist'}
            $launchArgs=@($argsObject.additional_arguments|Where-Object {$null -ne $_})
            if($launchArgs.Count){throw 'Application launch arguments are not enabled in the standalone bridge'}
            $argsObject.name=$resolved
            if(-not $launchArgs.Count){$argsObject.PSObject.Properties.Remove('additional_arguments')}
            $launchPreflight=$false
        }
        $desktopMove=($tool -ceq 'move_cursor' -and $argsObject.scope -ceq 'desktop')
        $desktopDrag=($tool -in @('drag','click') -and $argsObject.scope -ceq 'desktop')
        if($desktopMove){
            if(@($argsObject.PSObject.Properties|Where-Object {$_.Name -notin @('scope','x','y','cursor_id')}).Count){throw 'Unexpected desktop cursor arguments'}
            $screenCheck=Invoke-AcmaDriverCli $driver 'call get_screen_size --socket \\.\pipe\cua-driver' '{}'
            if($screenCheck.exit_code -ne 0){throw 'Cannot verify guest screen bounds'}
            $screen=$screenCheck.stdout|ConvertFrom-Json
            if($null -eq $argsObject.x -or $null -eq $argsObject.y -or [double]::IsNaN([double]$argsObject.x) -or [double]::IsNaN([double]$argsObject.y) -or $argsObject.x -lt 0 -or $argsObject.y -lt 0 -or $argsObject.x -ge $screen.width -or $argsObject.y -ge $screen.height){throw 'Cursor outside current guest screen'}
        }elseif ($tool -in $write) {
            if (($argsObject.scope -eq 'desktop' -and -not $desktopDrag) -or ($argsObject.target -and $argsObject.target.kind -ne 'window')) { throw 'Desktop operation not enabled' }
            if (-not $argsObject.pid -or -not $argsObject.window_id) { throw 'Explicit pid and window_id required for input' }
            $target = Get-Process -Id ([int]$argsObject.pid) -ErrorAction Stop
            $daemonSessions=@(Get-Process -Name cua-driver -ErrorAction Stop|Where-Object {$_.SessionId -gt 0}|Select-Object -ExpandProperty SessionId -Unique)
            if($target.SessionId -eq 0 -or $target.SessionId -notin $daemonSessions){throw 'Target is outside the Driver interactive guest session'}
            if(($target.ProcessName+'.exe') -iin $blockedApps){throw 'Input blocked by user application denylist'}
            $windowCheck=Invoke-AcmaDriverCli $driver 'call list_windows --socket \\.\pipe\cua-driver' '{}'
            if($windowCheck.exit_code -ne 0){throw 'Live target verification failed'}
            $observed=$windowCheck.stdout|ConvertFrom-Json
            if(@($observed.windows|Where-Object {$_.pid -eq $argsObject.pid -and $_.window_id -eq $argsObject.window_id}).Count -ne 1){throw 'Input requires an exact live interactive PID/HWND'}
            if ($argsObject.target -and ($argsObject.target.pid -ne $argsObject.pid -or $argsObject.target.window_id -ne $argsObject.window_id)) { throw 'Conflicting targets refused' }
            if($desktopDrag){
                if($argsObject.element_token -or $argsObject.modifier -or $argsObject.target){throw 'Desktop drag is coordinate-only and mouse-only'}
                $screenCheck=Invoke-AcmaDriverCli $driver 'call get_screen_size --socket \\.\pipe\cua-driver' '{}'
                if($screenCheck.exit_code -ne 0){throw 'Cannot verify desktop size'}
                $screen=$screenCheck.stdout|ConvertFrom-Json
                $pairs=if($tool -ceq 'click'){@{x='x';y='y'}}else{@{x='from_x';y='from_y'},@{x='to_x';y='to_y'}}
                foreach($pair in $pairs){
                    $x=$argsObject.($pair.x);$y=$argsObject.($pair.y)
                    if($null -eq $x -or $null -eq $y -or [double]::IsNaN([double]$x) -or [double]::IsNaN([double]$y) -or $x -lt 0 -or $y -lt 0 -or $x -ge $screen.width -or $y -ge $screen.height){throw 'Desktop drag endpoint outside guest screen'}
                }
                # PID/HWND bind ACMA authorization only. The native operation is
                # explicitly desktop-scoped, with no window activation request.
                $argsObject.PSObject.Properties.Remove('pid')
                $argsObject.PSObject.Properties.Remove('window_id')
            }
        }
        $inputJson = $argsObject | ConvertTo-Json -Depth 20 -Compress
        $arguments = 'call ' + $tool + ' --socket \\.\pipe\cua-driver'
    } else { throw 'Operation not allowed' }
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $driver
    $info.Arguments = $arguments
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardInput = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.StandardOutputEncoding = $utf8
    $info.StandardErrorEncoding = $utf8
    $child = [Diagnostics.Process]::new()
    $child.StartInfo = $info
    if (-not $child.Start()) { throw 'Could not start guest CLI' }
    $stdoutTask = $child.StandardOutput.ReadToEndAsync()
    $stderrTask = $child.StandardError.ReadToEndAsync()
    if ($inputJson) { $child.StandardInput.WriteLine($inputJson) }
    $child.StandardInput.Close()
    if (-not $child.WaitForExit(30000)) {
        # This terminates only our CLI client, not the daemon or a dispatched action.
        try { $child.Kill() } catch {}
        throw 'CLI timeout; desktop action outcome unknown; do not resend'
    }
    $stdout = $stdoutTask.GetAwaiter().GetResult()
    $stderr = $stderrTask.GetAwaiter().GetResult()
    if ($stdout.Length -gt 16777216 -or $stderr.Length -gt 65536) { throw 'Response too large; outcome must be reconciled' }
    $daemons = @(Get-Process -Name cua-driver -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $child.Id } | ForEach-Object { @{Id=$_.Id;SessionId=$_.SessionId;Path=$_.Path;IntegrityRid=(Get-AcmaIntegrityRid $_.Id)} })
    $reply = @{launch_failure_protocol='preflight-v1';application_policy_version='denylist-v1';request_id=$requestId;vm_id=$config.vm_id;computer=$env:COMPUTERNAME;user_sid=$identity.User.Value;bridge_session=[Diagnostics.Process]::GetCurrentProcess().SessionId;daemon_processes=$daemons;exit_code=$child.ExitCode;stdout=$stdout;stderr=$stderr}
    $child.Dispose()
    [Console]::WriteLine(($reply | ConvertTo-Json -Depth 20 -Compress))
} catch {
    $failure=@{request_id=$requestId;error=$_.Exception.Message;outcome='not_confirmed_no_automatic_retry'}
    if($launchPreflight){
        $failure.vm_id=$config.vm_id;$failure.computer=$env:COMPUTERNAME;$failure.user_sid=$identity.User.Value
        $failure.launch_failure_protocol='preflight-v1';$failure.phase='launch_preflight';$failure.code='launch_preflight_failed'
        $failure.dispatched=$false;$failure.outcome='rejected_before_dispatch'
    }
    [Console]::WriteLine(($failure | ConvertTo-Json -Compress))
    exit 1
}
