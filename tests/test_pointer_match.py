import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from acma_controller.pointer_match import match_element, verify_probe


class PointerMatchTests(unittest.TestCase):
    def setUp(self):
        self.expected = {'role': 'Edit', 'label': '文本编辑器', 'frame': {'x': 10, 'y': 20, 'w': 100, 'h': 80}}
        self.node = {'pid': 7, 'role': 'Pane', 'class': 'Edit', 'name': '', 'native_hwnd': 90,
                     'runtime_id': [42, 90], 'frame': dict(self.expected['frame'])}
        self.check = {'hit_pid': 7, 'hit_root': 8, 'hit_hwnd': 90, 'x': 60, 'y': 60, 'uia_chain': [self.node]}

    def test_provider_confirmation_refreshes_token_only_after_fresh_root_check(self):
        from acma_controller.control import GuestBackend
        from acma_controller.pointer_match import coarse_container_only
        source={'expected_bounds':{'x':0,'y':0,'width':200,'height':200},'points':[[60,60]],
                'expected_element':{'role':'Button','label':'Continue','frame':dict(self.expected['frame'])}}
        coarse={'confirmed':False,'reason':'element_identity_evidence_unconfirmed','target_identity':{
            'pid':7,'window_id':8,'exe_path':'C:/app.exe','session_id':1,'process_started_at':'start'},
            'checks':[{'x':60,'y':60,'hit_pid':7,'hit_root':8,'element_match':{'matching_candidates':0},
                       'uia_chain':[{'pid':7,'role':'Pane'}]}]}
        fresh={'pid':7,'window_id':8,'window_bounds':source['expected_bounds'],'elements_complete':True,
               'elements':[{'role':'Button','label':'Continue','frame':dict(self.expected['frame']),
                            'enabled':True,'actions':['invoke'],'element_token':'new:1'}]}
        class Backend:
            def __init__(self):self.reads=[];self.root_ok=True
            def cua(self,tool,args):
                from acma_controller.control import validate,VM_ID
                validate({'vm_id':VM_ID,'kind':'observe','tool':tool,'arguments':args})
                assert args.get('session')=='same-session'
                self.reads.append(tool);return fresh
            def pointer_probe(self,args,source):
                assert source['expected_element'] is None and source['expected_label'] is None
                return {'confirmed':self.root_ok,'target_identity':coarse['target_identity']}
        args={'pid':7,'window_id':8,'element_token':'old:1','session':'same-session'};backend=Backend()
        result=GuestBackend.confirm_pointer_provider(backend,copy.deepcopy(coarse),args,source)
        self.assertTrue(result['confirmed']);self.assertEqual(args['element_token'],'new:1')
        self.assertEqual(backend.reads,['get_window_state'])
        self.assertTrue(result['provider_confirmation']['read_only'])
        for change in [{'enabled':False},{'label':'Other'},{'frame':{'x':20,'y':20,'w':100,'h':80}}]:
            old=copy.deepcopy(fresh['elements'][0]);fresh['elements'][0].update(change)
            args['element_token']='old:1'
            self.assertFalse(GuestBackend.confirm_pointer_provider(backend,copy.deepcopy(coarse),args,source)['confirmed'])
            self.assertEqual(args['element_token'],'old:1');fresh['elements'][0]=old
        fresh['elements']*=2
        self.assertFalse(GuestBackend.confirm_pointer_provider(backend,copy.deepcopy(coarse),args,source)['confirmed'])
        fresh['elements']=fresh['elements'][:1];backend.root_ok=False
        self.assertFalse(GuestBackend.confirm_pointer_provider(backend,copy.deepcopy(coarse),args,source)['confirmed'])
        conflict=copy.deepcopy(coarse);conflict['checks'][0]['uia_chain'][0]['role']='Button'
        self.assertFalse(coarse_container_only(conflict,7,8))
        conflict=copy.deepcopy(coarse);conflict['checks'][0]['hit_root']=99
        self.assertFalse(coarse_container_only(conflict,7,8))

    def test_classic_edit_cross_provider_empty_name_matches(self):
        result = match_element(self.expected, self.check, 7, 8)
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['name_matches'])
        self.assertEqual(result['native_hwnd'], 90)

    def test_wrong_root_process_role_frame_name_or_hwnd_refused(self):
        for target, key, value in [('check','hit_root',9),('check','hit_pid',9),('node','pid',9),
                                   ('node','class','Other'),('node','native_hwnd',91),
                                   ('node','name','其他输入框'),('node','frame',{'x':11,'y':20,'w':50,'h':80})]:
            with self.subTest(key=key,value=value):
                check = copy.deepcopy(self.check)
                (check if target == 'check' else check['uia_chain'][0])[key] = value
                self.assertFalse(match_element(self.expected,check,7,8)['confirmed'])

    def test_named_uia_edit_and_small_rounding_difference(self):
        self.node.update(role='Edit', name='文本编辑器', native_hwnd=0, **{'class':'Custom'})
        self.node['frame']['x'] += 1
        self.assertTrue(match_element(self.expected,self.check,7,8)['confirmed'])

    def test_same_name_different_geometry_does_not_match(self):
        self.node.update(role='Edit',name='文本编辑器')
        self.node['frame']['x'] += 10
        self.assertFalse(match_element(self.expected,self.check,7,8)['confirmed'])

    def test_ambiguous_or_nonfinite_or_outside_refused(self):
        self.check['uia_chain'].append(copy.deepcopy(self.node))
        self.assertFalse(match_element(self.expected,self.check,7,8)['confirmed'])
        self.check['uia_chain'].pop()
        self.check['x'] = 500
        self.assertFalse(match_element(self.expected,self.check,7,8)['confirmed'])
        self.check['x'] = 60; self.node['frame']['x'] = float('nan')
        self.assertFalse(match_element(self.expected,self.check,7,8)['confirmed'])

    def test_no_fallback_on_window_geometry_or_probe_failure(self):
        for reason in ['observed_geometry_differs_from_current_win32_and_dwm_bounds','pointer_probe_failed']:
            data = {'confirmed':False,'reason':reason,'checks':[self.check]}
            self.assertFalse(verify_probe(data,{'expected_element':self.expected},7,8)['confirmed'])

    def test_probe_returns_explicit_match_evidence(self):
        data = {'confirmed':False,'reason':'hit_test_identity_occlusion_or_label_mismatch','checks':[self.check]}
        result=verify_probe(data,{'expected_element':self.expected},7,8)
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['checks'][0]['element_match']['runtime_id'],[42,90])
        self.assertIn('expected_element',result)

    def test_offscreen_element_reports_geometry_reason_instead_of_generic_hit_failure(self):
        self.check.update(y=850,hit_pid=0,hit_root=0,hit_hwnd=0,uia_chain=[])
        data={'confirmed':False,'reason':'hit_test_identity_occlusion_or_label_mismatch','checks':[self.check]}
        source={'expected_element':self.expected,'expected_bounds':{'x':0,'y':0,'width':1024,'height':728}}
        result=verify_probe(data,source,7,8)
        self.assertFalse(result['confirmed'])
        self.assertEqual(result['reason'],'target_outside_observed_window_bounds')


    def test_native_filename_text_name_requires_exact_value_and_direct_parent_label(self):
        self.expected.update(label='文件名:',value='*.txt')
        self.node['name']='*.txt'
        parent={'pid':7,'name':'文件名:','role':'Pane','class':'AppControlHost'}
        self.check['uia_chain'].append(parent)
        result=match_element(self.expected,self.check,7,8)
        self.assertTrue(result['confirmed'])
        self.assertEqual(result['label_source'],'native_value_and_direct_parent')
        for field,value in [('name','其他字段'),('pid',8)]:
            changed=copy.deepcopy(self.check);changed['uia_chain'][1][field]=value
            self.assertFalse(match_element(self.expected,changed,7,8)['confirmed'])
        self.expected['value']='changed.txt'
        self.assertFalse(match_element(self.expected,self.check,7,8)['confirmed'])

    def test_native_button_role_mapping_requires_handle_class_name_and_bounds(self):
        self.expected.update(role='Button',label='Save')
        self.node.update(name='Save',**{'class':'Button'})
        self.assertTrue(match_element(self.expected,self.check,7,8)['confirmed'])
        for key,value in [('native_hwnd',91),('class','Other'),('name','Cancel')]:
            c=copy.deepcopy(self.check);c['uia_chain'][0][key]=value
            self.assertFalse(match_element(self.expected,c,7,8)['confirmed'])


if __name__ == '__main__':
    unittest.main()
