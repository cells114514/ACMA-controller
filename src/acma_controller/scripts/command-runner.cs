// ACMA_COMMAND_API=2
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
    public bool stdout_truncated, stderr_truncated;
    public long stdout_bytes, stderr_bytes;
    public long elapsed_ms;
}
public static class AcmaCommandRunner {
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr attributes,string name);
    [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr job,IntPtr process);
    [DllImport("kernel32.dll")] static extern bool TerminateJobObject(IntPtr job,uint code);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll")] static extern bool SetInformationJobObject(IntPtr job,int type,IntPtr information,uint length);
    [StructLayout(LayoutKind.Sequential)] struct BasicLimits {public long processTime,jobTime;public uint flags;public UIntPtr minWorking,maxWorking;public uint activeProcesses;public UIntPtr affinity;public uint priority,scheduling;}
    [StructLayout(LayoutKind.Sequential)] struct IoCounters {public ulong readOps,writeOps,otherOps,readBytes,writeBytes,otherBytes;}
    [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits {public BasicLimits basic;public IoCounters io;public UIntPtr processMemory,jobMemory,peakProcess,peakJob;}
    static void KillOnClose(IntPtr job) {
        var limits=new ExtendedLimits();limits.basic.flags=0x2000;
        int length=Marshal.SizeOf(typeof(ExtendedLimits));IntPtr memory=Marshal.AllocHGlobal(length);
        try {Marshal.StructureToPtr(limits,memory,false);if(!SetInformationJobObject(job,9,memory,(uint)length))throw new InvalidOperationException("Cannot bind job lifetime");}
        finally {Marshal.FreeHGlobal(memory);}
    }
    sealed class Capture {
        public bool truncated;
        public long bytes;
        public string Read(StreamReader reader,int limit,string path) {
            var data=new MemoryStream();var buffer=new byte[4096];int n;var decoder=new UTF8Encoding(false).GetDecoder();var chars=new char[4098];
            using(var spool=new FileStream(path,FileMode.Create,FileAccess.Write,FileShare.ReadWrite)) {
                while((n=reader.BaseStream.Read(buffer,0,buffer.Length))>0) {
                    bytes+=n;int count=decoder.GetChars(buffer,0,n,chars,0,false);
                    byte[] encoded=Encoding.UTF8.GetBytes(chars,0,count);int keep=truncated?0:Math.Min(encoded.Length,Math.Max(0,limit-(int)data.Length));
                    if(keep<encoded.Length) {while(keep>0&&(encoded[keep]&0xc0)==0x80)keep--;}
                    if(keep>0){data.Write(encoded,0,keep);spool.Write(encoded,0,keep);spool.Flush();}
                    if(keep<encoded.Length&&!truncated){truncated=true;File.WriteAllText(path+".truncated","1");}
                }
            }
            return Encoding.UTF8.GetString(data.ToArray());
        }
    }
    public static AcmaCommandResult Run(string executable,string encoded,string cwd,string directory,int timeout,int limit) {
        var result=new AcmaCommandResult();
        IntPtr job=CreateJobObject(IntPtr.Zero,null);
        if(job==IntPtr.Zero)throw new InvalidOperationException("Cannot create command job");
        Process child=null;
        try {
            KillOnClose(job);
            var info=new ProcessStartInfo(executable,"-NoLogo -NoProfile -NonInteractive -OutputFormat Text -EncodedCommand "+encoded);
            info.WorkingDirectory=cwd; info.UseShellExecute=false; info.CreateNoWindow=true;
            info.RedirectStandardOutput=true;info.RedirectStandardError=true;
            info.StandardOutputEncoding=new UTF8Encoding(false);info.StandardErrorEncoding=new UTF8Encoding(false);
            child=Process.Start(info);result.child_pid=child.Id;
            // Child waits at a fixed start gate; no task code runs before job custody.
            if(!AssignProcessToJobObject(job,child.Handle)){child.Kill();child.WaitForExit();throw new InvalidOperationException("Cannot bind command process to job; no task admitted");}
            var stdout=new Capture();var stderr=new Capture();
            Task<string> outTask=Task.Run(()=>stdout.Read(child.StandardOutput,limit,Path.Combine(directory,"stdout.bin")));
            Task<string> errTask=Task.Run(()=>stderr.Read(child.StandardError,limit,Path.Combine(directory,"stderr.bin")));
            File.WriteAllText(Path.Combine(directory,"start.flag"),"admitted");
            var timer=Stopwatch.StartNew();
            while(!child.WaitForExit(100)){
                result.stopped=File.Exists(Path.Combine(directory,"cancel.flag"));
                result.timed_out=timeout>0&&timer.ElapsedMilliseconds>=timeout;
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
            result.stdout_truncated=stdout.truncated;result.stderr_truncated=stderr.truncated;
            result.stdout_bytes=stdout.bytes;result.stderr_bytes=stderr.bytes;
            result.elapsed_ms=timer.ElapsedMilliseconds;
            return result;
        } finally {if(job!=IntPtr.Zero){TerminateJobObject(job,253);CloseHandle(job);}if(child!=null)child.Dispose();}
    }
}
