# $probe is supplied by the controller as decoded JSON. Read-only interactive probe.
$ErrorActionPreference='Stop'
# Guest identity is checked by the pinned SSH entry before this read-only probe.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class AcmaPointerRead {
 [StructLayout(LayoutKind.Sequential)] public struct POINT {public int x,y;}
 [StructLayout(LayoutKind.Sequential)] public struct RECT {public int left,top,right,bottom;}
 [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(POINT point);
 [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr hwnd,uint flags);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hwnd,out uint pid);
 [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr hwnd);
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr hwnd);
 [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hwnd,out RECT rect);
 [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr hwnd,int attribute,out RECT rect,int size);
 [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr hwnd,ref POINT point);
 [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll",CharSet=CharSet.Unicode)] public static extern IntPtr FindWindow(string cls,string title);
 [DllImport("user32.dll")] public static extern int GetSystemMetrics(int index);
}
'@
$h=[IntPtr][long]$probe.window_id
$confirmed=$true;$reason='all_points_hit_exact_target_root';$checks=@()
$origin=New-Object AcmaPointerRead+POINT
$bounds=New-Object AcmaPointerRead+RECT
$dwmBounds=New-Object AcmaPointerRead+RECT
$geometry=@{}
[uint32]$ownerProcessId=0
if(-not $probe.pointer_only){
    if(-not [AcmaPointerRead]::IsWindow($h) -or -not [AcmaPointerRead]::IsWindowVisible($h)){throw 'Target window absent or hidden'}
    [AcmaPointerRead]::GetWindowThreadProcessId($h,[ref]$ownerProcessId)|Out-Null
    if($ownerProcessId -ne [uint32]$probe.pid){throw 'Target PID differs'}
    if(-not [AcmaPointerRead]::GetWindowRect($h,[ref]$bounds)){throw 'Cannot read current target rectangle'}
    $dwmOk=([AcmaPointerRead]::DwmGetWindowAttribute($h,9,[ref]$dwmBounds,16) -eq 0)
    if(-not [AcmaPointerRead]::ClientToScreen($h,[ref]$origin)){throw 'Cannot read target client origin'}
    if($probe.expected_bounds){
        $b=$probe.expected_bounds
        $win32Match=($bounds.left -eq $b.x -and $bounds.top -eq $b.y -and ($bounds.right-$bounds.left) -eq $b.width -and ($bounds.bottom-$bounds.top) -eq $b.height)
        $dwmMatch=($dwmOk -and $dwmBounds.left -eq $b.x -and $dwmBounds.top -eq $b.y -and ($dwmBounds.right-$dwmBounds.left) -eq $b.width -and ($dwmBounds.bottom-$dwmBounds.top) -eq $b.height)
        $geometry=@{expected=$b;win32=@{x=$bounds.left;y=$bounds.top;width=($bounds.right-$bounds.left);height=($bounds.bottom-$bounds.top)};
            dwm=$(if($dwmOk){@{x=$dwmBounds.left;y=$dwmBounds.top;width=($dwmBounds.right-$dwmBounds.left);height=($dwmBounds.bottom-$dwmBounds.top)}}else{$null});
            matched_api=$(if($win32Match){'GetWindowRect'}elseif($dwmMatch){'DwmExtendedFrameBounds'}else{'none'})}
        if(-not ($win32Match -or $dwmMatch)){
            @{confirmed=$false;reason='observed_geometry_differs_from_current_win32_and_dwm_bounds';geometry=$geometry;read_only=$true}|ConvertTo-Json -Depth 8 -Compress
            return
        }
    }
}
if($probe.expected_label -or $probe.expected_element){Add-Type -AssemblyName UIAutomationClient;Add-Type -AssemblyName UIAutomationTypes;Add-Type -AssemblyName WindowsBase}
if($probe.taskbar){
    # The taskbar is a separate Explorer root, not a child of Program Manager.
    # Resolve the fixed OS class in this interactive session, not an arbitrary
    # caller-supplied alternate HWND, and require the same authorized owner PID.
    $taskbar=[AcmaPointerRead]::FindWindow('Shell_TrayWnd',$null)
    [uint32]$taskbarPid=0
    if($taskbar -eq [IntPtr]::Zero -or -not [AcmaPointerRead]::IsWindowVisible($taskbar)){throw 'Visible taskbar absent'}
    [AcmaPointerRead]::GetWindowThreadProcessId($taskbar,[ref]$taskbarPid)|Out-Null
    if($taskbarPid -ne [uint32]$probe.pid){throw 'Taskbar differs from authorized Explorer process'}
    $geometry['surface']='taskbar';$geometry['taskbar_hwnd']=$taskbar.ToInt64()
    $h=$taskbar
}
foreach($pair in $probe.points){
    $point=New-Object AcmaPointerRead+POINT
    $point.x=[int][math]::Round([double]$pair[0]);$point.y=[int][math]::Round([double]$pair[1])
    if($probe.space -ceq 'window_client'){$point.x+=$origin.x;$point.y+=$origin.y}
    if($probe.pointer_only){
        $ok=($point.x -ge 0 -and $point.y -ge 0 -and $point.x -lt [AcmaPointerRead]::GetSystemMetrics(0) -and $point.y -lt [AcmaPointerRead]::GetSystemMetrics(1))
        $checks+=@{x=$point.x;y=$point.y;confirmed=$ok;scope='registered_guest_primary_screen'}
    }else{
        $hit=[AcmaPointerRead]::WindowFromPoint($point)
        $root=[AcmaPointerRead]::GetAncestor($hit,2)
        [uint32]$hitProcessId=0
        [AcmaPointerRead]::GetWindowThreadProcessId($root,[ref]$hitProcessId)|Out-Null
        $ok=($root -eq $h -and $hitProcessId -eq [uint32]$probe.pid)
        $names=@();$labelMatched=$null
        if($ok -and ($probe.expected_label -or $probe.expected_element)){
            $labelMatched=$false
            $node=[System.Windows.Automation.AutomationElement]::FromPoint([System.Windows.Point]::new($point.x,$point.y))
            for($i=0;$node -and $i -lt 8;$i++){
                $c=$node.Current;$rect=$c.BoundingRectangle
                $names+=@{name=$c.Name;pid=$c.ProcessId;class=$c.ClassName;
                    role=($c.ControlType.ProgrammaticName -replace '^ControlType\.','');native_hwnd=[long]$c.NativeWindowHandle;
                    runtime_id=@($node.GetRuntimeId());frame=@{x=$rect.X;y=$rect.Y;w=$rect.Width;h=$rect.Height}}
                if($c.Name -ceq [string]$probe.expected_label){$labelMatched=$true}
                $node=[System.Windows.Automation.TreeWalker]::RawViewWalker.GetParent($node)
            }
            $ok=$ok -and $labelMatched
        }
        $checks+=@{x=$point.x;y=$point.y;hit_hwnd=$hit.ToInt64();hit_root=$root.ToInt64();hit_pid=$hitProcessId;
            confirmed=$ok;label_matched=$labelMatched;uia_chain=$names}
    }
    if(-not $ok){$confirmed=$false;$reason='hit_test_identity_occlusion_or_label_mismatch'}
}
$targetIdentity=$null
if($probe.pid -and $probe.window_id){
    try{
        $targetProcess=Get-CimInstance Win32_Process -Filter "ProcessId=$([long]$probe.pid)"
        $targetIdentity=@{pid=[long]$probe.pid;window_id=[long]$probe.window_id;exe_path=$targetProcess.ExecutablePath;
            session_id=$targetProcess.SessionId;process_started_at=$targetProcess.CreationDate.ToString('o');checked_at_utc=[DateTime]::UtcNow.ToString('o')}
    }catch{$targetIdentity=$null}
}
@{confirmed=$confirmed;reason=$reason;checks=$checks;geometry=$geometry;session=[Diagnostics.Process]::GetCurrentProcess().SessionId;target_identity=$targetIdentity;
    foreground_hwnd=[AcmaPointerRead]::GetForegroundWindow().ToInt64();checked_at_utc=[DateTime]::UtcNow.ToString('o');
    read_only=$true}|ConvertTo-Json -Depth 10 -Compress
