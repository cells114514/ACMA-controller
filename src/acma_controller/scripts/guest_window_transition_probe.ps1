# Read-only, interactive-guest ownership probe. $probe is controller supplied.
$ErrorActionPreference='Stop'
# Guest identity is checked by the pinned SSH entry before this read-only probe.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;
public static class AcmaTransitionRead {
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
 [DllImport("user32.dll")] public static extern IntPtr GetWindow(IntPtr hwnd,uint command);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd,out uint pid);
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern int GetClassName(IntPtr hwnd,StringBuilder text,int count);
}
'@
function Read-Window([long]$windowId){
    $handle=[IntPtr]$windowId
    [uint32]$processId=0
    [AcmaTransitionRead]::GetWindowThreadProcessId($handle,[ref]$processId)|Out-Null
    if($processId -eq 0){return @{window_id=$windowId;pid=0;visible=$false;owner_chain=@()}}
    $process=Get-CimInstance Win32_Process -Filter "ProcessId=$processId"
    $class=[Text.StringBuilder]::new(256)
    [AcmaTransitionRead]::GetClassName($handle,$class,256)|Out-Null
    $owners=@();$current=$handle
    for($i=0;$i -lt 12;$i++){
        $current=[AcmaTransitionRead]::GetWindow($current,4)
        if($current -eq [IntPtr]::Zero -or $owners -contains $current.ToInt64()){break}
        $owners+=$current.ToInt64()
    }
    return @{window_id=$windowId;pid=$processId;exe_path=$process.ExecutablePath;
        session_id=$process.SessionId;parent_pid=$process.ParentProcessId;
        visible=[AcmaTransitionRead]::IsWindowVisible($handle);class=$class.ToString();owner_chain=$owners}
}
@{read_only=$true;foreground_hwnd=[AcmaTransitionRead]::GetForegroundWindow().ToInt64();target=(Read-Window ([long]$probe.window_id));candidate=(Read-Window ([long]$probe.candidate_window_id))}|ConvertTo-Json -Depth 8 -Compress
