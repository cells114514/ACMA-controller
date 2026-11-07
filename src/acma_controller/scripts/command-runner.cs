using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading.Tasks;

public sealed class AcmaCommandResult {
    public string stdout, stderr;
    public int exit_code, child_pid;
    public bool timed_out, stopped, output_truncated, tree_terminated;
}
public static class AcmaCommandRunner {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr attributes,string name);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
    [DllImport("kernel32.dll")] static extern bool TerminateJobObject(IntPtr job,uint code);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    sealed class Capture {
        public bool truncated;
        public string Read(StreamReader reader,int limit) {
            var data=new StringBuilder(); var buffer=new char[4096]; int n;
            while((n=reader.Read(buffer,0,buffer.Length))>0){int keep=Math.Min(n,Math.Max(0,limit-data.Length));if(keep>0)data.Append(buffer,0,keep);if(keep<n)truncated=true;}
            return data.ToString();
        }
    }
    public static AcmaCommandResult Run(string executable,string encoded,string cwd,string directory,int timeout,int limit) {
        var result=new AcmaCommandResult();
        IntPtr job=CreateJobObject(IntPtr.Zero,null);
        if(job==IntPtr.Zero)throw new InvalidOperationException("Cannot create command job");
        Process child=null;
        try {
            var info=new ProcessStartInfo(executable,"-NoLogo -NoProfile -NonInteractive -OutputFormat Text -EncodedCommand "+encoded);
            info.WorkingDirectory=cwd; info.UseShellExecute=false; info.CreateNoWindow=true;
            info.RedirectStandardOutput=true;info.RedirectStandardError=true;
            info.StandardOutputEncoding=new UTF8Encoding(false);info.StandardErrorEncoding=new UTF8Encoding(false);
            child=Process.Start(info);result.child_pid=child.Id;
            // Child waits at a fixed start gate; no task code runs before job custody.
            if(!AssignProcessToJobObject(job,child.Handle)){child.Kill();child.WaitForExit();throw new InvalidOperationException("Cannot bind command process to job; no task admitted");}
            var stdout=new Capture();var stderr=new Capture();
            Task<string> outTask=Task.Run(()=>stdout.Read(child.StandardOutput,limit));
            Task<string> errTask=Task.Run(()=>stderr.Read(child.StandardError,limit));
            File.WriteAllText(Path.Combine(directory,"start.flag"),"admitted");
            var timer=Stopwatch.StartNew();
            while(!child.WaitForExit(100)){
                result.stopped=File.Exists(Path.Combine(directory,"cancel.flag"));
                result.timed_out=timer.ElapsedMilliseconds>=timeout;
                if(result.stopped||result.timed_out){if(!TerminateJobObject(job,253))throw new InvalidOperationException("Command job termination failed; outcome unknown");break;}
            }
            if(!child.WaitForExit(5000))throw new InvalidOperationException("Command process exit not confirmed");
            result.exit_code=child.ExitCode;
            // Also terminate any descendants that outlived their command parent.
            result.tree_terminated=TerminateJobObject(job,253);
            if(!result.tree_terminated)throw new InvalidOperationException("Command descendants termination not confirmed; outcome unknown");
            if(!Task.WaitAll(new Task[]{outTask,errTask},5000))throw new InvalidOperationException("Command output pipes did not close; outcome unknown");
            result.stdout=outTask.Result;result.stderr=errTask.Result;
            result.output_truncated=stdout.truncated||stderr.truncated;
            return result;
        } finally {if(job!=IntPtr.Zero){TerminateJobObject(job,253);CloseHandle(job);}if(child!=null)child.Dispose();}
    }
}
