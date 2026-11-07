# Host-only fixed-identity Hyper-V adapter. No force, TurnOff, Reset or key access.
param([Parameter(Mandatory)][string]$RequestPath,[Parameter(Mandatory)][string]$ResultPath)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$r=Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8|ConvertFrom-Json
$report=[ordered]@{operation_id=$r.operation_id;vm_id=$r.vm_id;tool=$r.tool;state='refused';dispatched=$false;started_at=$r.started_at}
$sent=$false
function Read-VMState($machine) {
    [ordered]@{name=$machine.Name;id=[string]$machine.Id;state=[string]$machine.State;status=[string]$machine.Status;
        uptime_seconds=$machine.Uptime.TotalSeconds;addresses=@(Get-VMNetworkAdapter -VM $machine|ForEach-Object {$_.IPAddresses})}
}
function Wait-VMState([string]$wanted) {
    $end=[DateTime]::UtcNow.AddSeconds(120)
    do {
        $machine=Get-VM -Id $r.vm_id
        if([string]$machine.State -eq $wanted){return $machine}
        Start-Sleep -Milliseconds 500
    } while([DateTime]::UtcNow -lt $end)
    throw "Expected $wanted was not observed; no retry or force action."
}
try {
    if(-not [guid]::TryParse([string]$r.vm_id,[ref]([guid]::Empty))){throw 'Invalid configured VM ID'}
    Import-Module Hyper-V
    $vm=Get-VM -Id $r.vm_id
    if($vm.Name -cne $r.vm_name){throw 'VM name mismatch'}
    $report.before=Read-VMState $vm
    if($r.tool -notin @('vm_status','vm_list_checkpoints')) {
        if([string]$vm.State -notin @('Running','Off','Saved')){throw 'VM is transitioning; observe without sending lifecycle actions.'}
    }
    switch -Exact ($r.tool) {
        'vm_status' { }
        'vm_list_checkpoints' { $report.checkpoints=@(Get-VMSnapshot -VM $vm|ForEach-Object {
            [ordered]@{id=[string]$_.Id;name=$_.Name;creation_time=$_.CreationTime.ToString('o');type=[string]$_.SnapshotType}
        }) }
        'vm_start' {
            if($vm.State -ne 'Off'){throw 'vm_start requires Off; use vm_resume for Saved.'}
            $sent=$true;Start-VM -VM $vm|Out-Null;$vm=Wait-VMState 'Running'
        }
        'vm_resume' {
            if($vm.State -ne 'Saved'){throw 'vm_resume requires Saved.'}
            $sent=$true;Start-VM -VM $vm|Out-Null;$vm=Wait-VMState 'Running'
        }
        'vm_shutdown' {
            if($vm.State -ne 'Running'){throw 'vm_shutdown requires Running.'}
            $sent=$true;Stop-VM -VM $vm -Confirm:$false;$vm=Wait-VMState 'Off'
        }
        'vm_restart' {
            if($vm.State -ne 'Running'){throw 'vm_restart requires Running.'}
            $sent=$true;Stop-VM -VM $vm -Confirm:$false;$vm=Wait-VMState 'Off'
            Start-VM -VM $vm|Out-Null;$vm=Wait-VMState 'Running'
        }
        'vm_save' {
            if($vm.State -ne 'Running'){throw 'vm_save requires Running.'}
            $sent=$true;Save-VM -VM $vm;$vm=Wait-VMState 'Saved'
        }
        'vm_create_checkpoint' {
            if(@(Get-VMSnapshot -VM $vm|Where-Object {$_.Name -ceq $r.arguments.name}).Count){throw 'Checkpoint name already exists; no duplicate created.'}
            $sent=$true;$snapshot=Checkpoint-VM -VM $vm -SnapshotName $r.arguments.name -Passthru
            $report.checkpoint_id=[string]$snapshot.Id
            if(@(Get-VMSnapshot -VM $vm|Where-Object {$_.Id -eq $snapshot.Id}).Count -ne 1){throw 'Created checkpoint could not be independently listed; do not repeat.'}
        }
        'vm_restore_checkpoint' {
            if($vm.State -ne 'Off'){throw 'Restore requires Off. No automatic shutdown or start.'}
            $snapshot=Get-VMSnapshot -VM $vm|Where-Object {$_.Id -eq [guid]$r.arguments.checkpoint_id}
            if(@($snapshot).Count -ne 1){throw 'Exact checkpoint ID not found for ACMA.'}
            $sent=$true;Restore-VMSnapshot -VMSnapshot $snapshot -Confirm:$false
            $report.checkpoint_id=[string]$snapshot.Id
        }
        default { throw 'Unsupported VM operation' }
    }
    $report.after=Read-VMState (Get-VM -Id $r.vm_id)
    $report.state='succeeded'
} catch {
    $report.state=if($sent){'unknown'}else{'refused'}
    $report.error=$_.Exception.Message
} finally {
    $report.dispatched=$sent
    $report.finished_at=[DateTimeOffset]::Now.ToString('o')
    $report.retry_action=$false
    $tmp=$ResultPath+'.tmp'
    $report|ConvertTo-Json -Depth 8|Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $ResultPath -Force
}
