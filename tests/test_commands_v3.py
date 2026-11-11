import json
from pathlib import Path
import tempfile
import unittest
from acma_controller.sdk import Controller
from acma_controller import control


class Backend:
    def __init__(self):self.jobs={};self.calls=[];self.version=2
    def prepare_transport(self):return {'verified':True}
    def request(self,op,**kwargs):
        self.calls.append((op,kwargs));args=kwargs.get('arguments',{})
        if op=='command_capabilities':return {'command_api_version':self.version}
        identifier=args['job_id']
        if op=='command_start':
            self.jobs[identifier]={'state':'running' if args.get('persistent') else 'succeeded','stdout':'{"ok":true}',
                'stderr':'','process_started':True,'exit_code':None if args.get('persistent') else 0,'exit_confirmed':not args.get('persistent')}
            return {'command':{'state':'starting'},'response_file':'fake'}
        job=self.jobs[identifier]
        if op=='command_cancel':job.update(state='stop_requested');return {'command':dict(job),'response_file':'fake'}
        if job['state']=='stop_requested':job.update(state='stopped',exit_confirmed=True,tree_terminated=True)
        return {'command':dict(job),'response_file':'fake'}
    def desktop_input(self,args):self.calls.append(('desktop',args));return {'effect':'unverifiable'}
    def cua(self,name,args):
        if getattr(self,'desktop_unavailable',False):raise ValueError('Desktop is not interactive')
        return {'elements':[]}


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.backend=Backend();self.directory=Path(self.temp.name)
        self.c=Controller(backend=self.backend,directory=self.directory);self.c.start();self.c.resume();self.addCleanup(self.close)
    def close(self):
        if self.c.started:self.c.close()
    def test_v2_command_cwd_env_and_old_protocol_refusal(self):
        self.backend.desktop_unavailable=True
        row=self.c.call('run_command',{'command':'test','cwd':r'C:\Windows','env':{'X':'中文'}})
        self.assertEqual(row['state'],'succeeded');self.assertEqual(row['result_json'],{'ok':True})
        args=next(x[1]['arguments'] for x in self.backend.calls if x[0]=='command_start')
        self.assertEqual(args['cwd'],r'C:\Windows');self.assertEqual(args['env'],{'X':'中文'})
        self.backend.calls.clear();self.backend.version=1
        row=self.c.call('run_command',{'command':'new'})
        self.assertEqual(row['state'],'refused');self.assertFalse(row['dispatched'])
        self.assertFalse(any(x[0]=='command_start' for x in self.backend.calls))
    def test_shell_survives_controller_close_and_stops_by_original_id(self):
        row=self.c.call('shell_start',{'command':'loop'});identifier=row['shell_id'];origin=row['origin_task_id']
        self.assertEqual(row['verification']['status'],'waiting')
        with self.assertRaises(ValueError):self.c.call('shell_start',{'command':'loop'})
        self.c.close();self.c=Controller(backend=self.backend,directory=self.directory);self.c.start();self.c.resume()
        status=self.c.call('shell_status',{'shell_id':identifier,'stdout_offset':0,'max_bytes':4})
        self.assertEqual(status['shell_state'],'running');self.assertEqual(status['origin_task_id'],origin)
        self.assertEqual(self.c.call('shell_stop',{'shell_id':identifier})['verification']['status'],'waiting')
        status=self.c.call('shell_status',{'shell_id':identifier})
        self.assertEqual(status['shell_state'],'stopped');self.assertTrue(status['tree_terminated'])
        self.assertEqual(self.c.store.get(identifier)['task_id'],origin)
        self.assertEqual(len([x for x in self.backend.calls if x[0]=='command_start']),1)
    def test_resource_kind_and_cursors_rejected_before_dispatch(self):
        row=self.c.call('run_command',{'command':'test'})
        before=len(self.backend.calls)
        with self.assertRaises(ValueError):self.c.call('shell_status',{'shell_id':row['action_id']})
        row=self.c.call('shell_start',{'command':'loop'})
        with self.assertRaises(ValueError):self.c.call('shell_status',{'shell_id':row['shell_id'],'stdout_offset':-1})
        with self.assertRaises(ValueError):self.c.call('command_cancel',{'action_id':row['shell_id']})
    def test_global_key_has_no_window_activation_and_text_stays_bound(self):
        row=self.c.call('hotkey',{'keys':['win','d']})
        self.assertEqual(row['state'],'unverified')
        sent=next(x[1] for x in self.backend.calls if x[0]=='desktop')
        self.assertEqual(sent,{'action':'key','key':'d','modifiers':['win']})
        with self.assertRaises(ValueError):self.c.call('type_text',{'text':'hello'})
    def test_windowless_mouse_uses_fresh_capture_and_never_activates(self):
        source=self.c.submit({'vm_id':self.c.config.vm_id,'kind':'observe','tool':'get_desktop_state','arguments':{}});self.c.wait(source,2)
        from datetime import datetime,timezone,timedelta
        state={'capture_id':source,'image_width':400,'image_height':200,'observed_at':datetime.now(timezone.utc).isoformat()}
        self.c.store.finish(source,'succeeded',state)
        row=self.c.call('desktop_mouse',{'action':'click','capture':source,'coordinate_space':'screenshot_pixels','x':50,'y':60})
        self.assertEqual(row['state'],'unverified');self.assertIn('post_readback',row)
        self.assertEqual(next(x[1] for x in self.backend.calls if x[0]=='desktop'),{'action':'click','x':50,'y':60})
        self.assertFalse(any(x[0]=='bring_to_front' for x in self.backend.calls))
        with self.assertRaises(ValueError):self.c.call('desktop_mouse',{'action':'click','capture':source,'coordinate_space':'screenshot_pixels','x':500,'y':60})
        self.c.store.finish(source,'succeeded',{**state,'observed_at':(datetime.now(timezone.utc)-timedelta(seconds=60)).isoformat()})
        with self.assertRaises(ValueError):self.c.call('desktop_mouse',{'action':'click','capture':source,'coordinate_space':'screenshot_pixels','x':50,'y':60})
    def test_unknown_command_reconciliation_queries_original_job(self):
        spec={'vm_id':self.c.config.vm_id,'kind':'command','command_v2':True,'background':False,'script':'once','cwd':self.c.config.guest_workspace,'env':{},'timeout_ms':1000,'output_limit':1024}
        identifier=self.c.submit(spec);self.c.wait(identifier,2)
        self.c.store.finish(identifier,'unknown',{})
        result=self.c.call('command_status',{'action_id':identifier})
        self.assertEqual(result['state'],'succeeded');self.assertEqual(result['action_id'],identifier)
        self.assertEqual(len([x for x in self.backend.calls if x[0]=='command_start']),1)
    def test_unknown_running_command_status_and_cancel_keep_resource_state(self):
        row=self.c.call('run_command',{'command':'once'});identifier=row['action_id']
        self.c.store.finish(identifier,'unknown',{})
        self.backend.jobs[identifier].update(state='running',exit_confirmed=False)
        result=self.c.call('command_status',{'action_id':identifier})
        self.assertEqual(result['state'],'running');self.assertEqual(result['action_id'],identifier)
        self.assertEqual(self.c.call('command_cancel',{'action_id':identifier})['state'],'cancel_requested')
        result=self.c.call('command_status',{'action_id':identifier})
        self.assertEqual(result['state'],'stopped');self.assertTrue(result['exit_confirmed'])
        self.assertEqual(len([x for x in self.backend.calls if x[0]=='command_start']),1)
