import json
from pathlib import Path
import tempfile
import threading
import subprocess
import sys
import os
import unittest
from unittest.mock import patch
from urllib.request import Request,urlopen
from urllib.error import HTTPError

from acma_controller.sdk import Controller
from acma_controller.config import Config
from acma_controller.server import make_server


class Backend:
    def __init__(self):self.calls=[]
    def prepare_transport(self):self.calls.append('prepare');return {}
    def cua(self,name,args):
        self.calls.append(name)
        if name=='list_apps':return {'apps':[{'name':'example.exe','running':True}]}
        if name=='get_window_state':return {**args,'elements':[],'snapshot_id':'snapshot','elements_complete':True,'truncated':False}
        return {'effect':'confirmed'}


class SDKTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.backend=Backend()
        self.controller=Controller(backend=self.backend,directory=self.root)
        self.controller.start();self.addCleanup(self.controller.close)
    def test_resume_without_guest_and_lazy_first_observation(self):
        self.controller.resume();self.assertEqual(self.backend.calls,[])
        row=self.controller.call('list_apps',{})
        self.assertEqual(row['state'],'succeeded');self.assertEqual(self.backend.calls[0],'prepare')
    def test_configured_vm_tools_have_no_model_dependency(self):
        self.controller.resume()
        with patch.object(self.controller.vm,'call',return_value={'state':'succeeded','after':{'state':'Running'}}) as invoke:
            self.assertEqual(self.controller.call('vm_start')['state'],'succeeded')
            invoke.assert_called_once()
        self.assertEqual(self.backend.calls,[])
    def test_foreign_target_and_stale_token_refused(self):
        self.controller.resume()
        with self.assertRaises(ValueError):self.controller.submit({'vm_id':'host','kind':'observe','tool':'list_apps','arguments':{}})
        with self.assertRaisesRegex(ValueError,'reference'):
            self.controller.call('click',{'pid':10,'window_id':20,'element_token':'old:0'})
        self.assertEqual(self.backend.calls,[])
    def test_deterministic_element_search(self):
        self.controller.resume()
        result=self.controller.call('find_elements',{'pid':10,'window_id':20,'query':'missing'})
        self.assertEqual(result['status'],'not_found');self.assertTrue(result['search_complete'])
    def test_wait_does_not_resend_after_queue_timeout(self):
        with patch.object(self.controller.store,'get',return_value={'id':'existing','state':'running'}),patch.object(self.controller,'submit') as submit:
            result=self.controller.wait('existing',seconds=.01)
            self.assertTrue(result['wait_expired']);self.assertFalse(result['retry_action'])
            submit.assert_not_called()

    def test_loopback_auth_and_request_deduplication(self):
        token='x'*40;server=make_server(self.controller,token,0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        url=f'http://127.0.0.1:{server.server_port}'
        with self.assertRaises(HTTPError) as error:urlopen(url+'/v1/state')
        self.assertEqual(error.exception.code,403)
        def post(body):
            req=Request(url+'/v1/call',data=json.dumps(body).encode(),headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
            return json.load(urlopen(req))
        body={'request_id':'same-request','tool':'vm_status','arguments':{}}
        with patch.object(self.controller,'call',return_value={'state':'succeeded'}) as call:
            self.assertEqual(post(body)['result']['state'],'succeeded')
            self.assertTrue(post(body)['replayed_result_only']);call.assert_called_once()
            with self.assertRaises(HTTPError):post({**body,'tool':'vm_start'})
        req=Request(url+'/v1/state',headers={'Authorization':'Bearer '+token,'Origin':'https://example.invalid'})
        with self.assertRaises(HTTPError):urlopen(req)

    def test_example_is_valid_and_unknown_config_field_rejected(self):
        example=Path(__file__).resolve().parents[1]/'config.example.json'
        config=Config.load(example);self.assertEqual(config.guest_workspace,r'C:\ACMA\Workspace')
        value=json.loads(example.read_text());value['host_shell']=True
        path=self.root/'bad.json';path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):Config.load(path)

    def test_cli_tool_catalog_is_utf8_json_without_guest_connection(self):
        root=Path(__file__).resolve().parents[1]
        env={**os.environ,'PYTHONPATH':str(root/'src'),'ACMA_CONTROLLER_HOME':str(self.root/'cli')}
        result=subprocess.run([sys.executable,'-m','acma_controller','--config',str(root/'config.example.json'),'tools'],
                              capture_output=True,encoding='utf-8',env=env,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        catalog=json.loads(result.stdout)
        self.assertEqual(len(catalog),39)
        self.assertTrue(all('parameters' in t for t in catalog))

    def test_runtime_is_pinned_to_a_single_vm(self):
        self.controller.close()
        with self.controller.store.db() as db:
            db.execute("UPDATE settings SET value='00000000-0000-4000-8000-000000000099' WHERE key='registered_vm_id'")
        with self.assertRaisesRegex(ValueError,'another VM'):Controller(backend=self.backend,directory=self.root)

    def test_batch_reuses_real_queue_and_returns_individual_receipts(self):
        self.controller.resume()
        result=self.controller.call('execute_batch',{'actions':[{'tool':'list_apps','arguments':{}},{'tool':'get_screen_size','arguments':{}}]})
        self.assertEqual(result['status'],'completed');self.assertEqual(result['completed_count'],2)
        self.assertTrue(all(s['result']['id'] for s in result['steps']))

    def test_screenshot_view_is_registered_and_does_not_refresh_capture_age(self):
        from PIL import Image
        self.controller.resume()
        row=self.controller.call('get_window_state',{'pid':10,'window_id':20,'include_screenshot':True})
        source=self.controller.store.directory/'test-source.png';Image.new('RGB',(2400,1600),'blue').save(source)
        result={**row['result'],'image_path':str(source),'capture_id':'driver-capture','screenshot_width':2400,'screenshot_height':1600,
                'observed_at':'2020-01-01T00:00:00+00:00'}
        self.controller.store.finish(row['id'],'succeeded',result)
        view=self.controller.call('screenshot_view',{'capture':row['id'],'region':[100,200,500,600]})
        self.assertEqual(view['source_capture'],row['id'])
        with self.assertRaisesRegex(ValueError,'expired'):
            self.controller.call('click',{'pid':10,'window_id':20,'x':10,'y':10,'coordinate_space':'view_pixels','view_id':view['view_id']},capture=row['id'])


if __name__=='__main__':unittest.main()
