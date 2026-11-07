import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from acma_controller.input_profile import bind
from acma_controller.control import Store,Worker,VM_ID,now,GuestBackend


class Fake:
    def __init__(self):self.calls=[];self.hit=True
    def cua(self,tool,args):
        self.calls.append((tool,args))
        if tool=='list_windows':return {'windows':[{'pid':1,'window_id':2,'bounds':{'x':0,'y':0,'width':100,'height':100}}]}
        if tool=='bring_to_front':return {'landed_on_target':True,'now_fg_hwnd':'0x2'}
        return {'effect':'confirmed'}
    def pointer_probe(self,args,source):
        self.calls.append(('probe',source));return {'confirmed':self.hit}


class InputProfileTests(unittest.TestCase):
    def test_inference_none_mouse_keyboard_and_mixed(self):
        for tool,expected,args in [('drag',(True,False),{}),('type_text',(False,True),{}),('click',(True,True),{'modifier':'ctrl'}),('get_window_state',(False,False),{})]:
            spec={'kind':'observe' if tool=='get_window_state' else 'desktop','tool':tool,'arguments':args}
            self.assertEqual(tuple(bind(spec).values()),expected)
        self.assertEqual(bind({'kind':'command'}),{'mouse':False,'keyboard':False})
    def test_forged_or_non_boolean_declaration_rejected(self):
        for value in ({'mouse':True,'keyboard':False},{'mouse':0,'keyboard':1},{'keyboard':True},'keyboard'):
            with self.assertRaises(ValueError):bind({'kind':'desktop','tool':'type_text','input':value})
    def setup_queue(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        s=Store(temporary.name);s.set_mode('running');backend=Fake();worker=Worker(s,backend)
        return s,backend,worker
    def capture(self,s):
        cap=s.submit({'vm_id':VM_ID,'kind':'observe','tool':'get_window_state','arguments':{}})
        s.finish(cap,'succeeded',{'capture_id':'image','pid':1,'window_id':2,'screenshot_width':100,'screenshot_height':100,
            'window_bounds':{'x':0,'y':0,'width':100,'height':100},'observed_at':now(),
            'elements':[{'frame':{'x':1,'y':1,'w':50,'h':50},'screenshot_frame':{'x':0,'y':0,'w':50,'h':50}}]})
        return cap
    def test_mouse_skips_keyboard_focus_but_requires_hit_check(self):
        s,b,w=self.setup_queue();cap=self.capture(s)
        spec={'vm_id':VM_ID,'kind':'desktop','tool':'drag','capture':cap,'arguments':{'pid':1,'window_id':2,'from_x':10,'from_y':10,'to_x':70,'to_y':10}}
        action=s.submit(spec);w.tick();r=s.get(action)
        self.assertEqual(r['spec']['input'],{'mouse':True,'keyboard':False})
        self.assertNotIn('input',spec)
        self.assertNotIn('bring_to_front',[t for t,a in b.calls])
        self.assertIn('probe',[t for t,a in b.calls]);self.assertEqual(b.calls[-1][1]['delivery_mode'],'background')
        self.assertEqual(r['result']['dispatch']['guard'],'mouse_hit')
        self.assertEqual(r['result']['dispatch']['delivery_mode'],'background')
        source=next(a for t,a in b.calls if t=='probe')
        self.assertEqual(source['points'],[[11,11],[71,11]])

    def desktop_capture(self,s):
        cap=s.submit({'vm_id':VM_ID,'kind':'observe','tool':'get_desktop_state','arguments':{}})
        s.finish(cap,'succeeded',{'capture_id':'desktop','screen_width':100,'screen_height':100,'observed_at':now()})
        return cap

    def test_desktop_drag_uses_screen_points_without_window_activation(self):
        s,b,w=self.setup_queue();self.capture(s);cap=self.desktop_capture(s)
        spec={'vm_id':VM_ID,'kind':'desktop','tool':'drag','capture':cap,'arguments':{'scope':'desktop','pid':1,'window_id':2,'from_x':10,'from_y':10,'to_x':70,'to_y':10}}
        identifier=s.submit(spec);w.tick();self.assertEqual(s.get(identifier)['state'],'succeeded')
        self.assertNotIn('bring_to_front',[t for t,a in b.calls])
        self.assertEqual(next(a for t,a in b.calls if t=='probe')['points'],[[10,10],[70,10]])
        self.assertEqual(b.calls[-1][1]['scope'],'desktop')
        self.assertNotIn('delivery_mode',b.calls[-1][1])
        self.assertEqual(s.get(identifier)['spec']['input'],{'mouse':True,'keyboard':False})

    def test_desktop_drag_requires_desktop_capture_and_recent_target(self):
        s,b,w=self.setup_queue();window=self.capture(s);desktop=self.desktop_capture(s)
        spec={'vm_id':VM_ID,'kind':'desktop','tool':'drag','capture':window,'arguments':{'scope':'desktop','pid':1,'window_id':2,'from_x':10,'from_y':10,'to_x':70,'to_y':10}}
        with self.assertRaisesRegex(ValueError,'desktop capture'):s.submit(spec)
        spec['capture']=desktop;spec['arguments']['pid']=9
        with self.assertRaisesRegex(ValueError,'fresh observed'):s.submit(spec)
        spec['arguments']['pid']=1;spec['arguments']['delivery_mode']='foreground'
        with self.assertRaisesRegex(ValueError,'window delivery mode'):s.submit(spec)
        spec['arguments'].pop('delivery_mode');spec['arguments']['modifier']=['ctrl']
        with self.assertRaisesRegex(ValueError,'mouse-only'):s.submit(spec)

    def test_desktop_drag_occlusion_refuses_before_dispatch(self):
        s,b,w=self.setup_queue();self.capture(s);cap=self.desktop_capture(s);b.hit=False
        identifier=s.submit({'vm_id':VM_ID,'kind':'desktop','tool':'drag','capture':cap,'arguments':{'scope':'desktop','pid':1,'window_id':2,'from_x':10,'from_y':10,'to_x':70,'to_y':10}})
        w.tick();self.assertEqual(s.get(identifier)['state'],'refused');self.assertNotIn('drag',[t for t,a in b.calls])

    def test_desktop_click_keeps_hit_guard_and_uses_screen_coordinates(self):
        s,b,w=self.setup_queue();self.capture(s);cap=self.desktop_capture(s)
        identifier=s.submit({'vm_id':VM_ID,'kind':'desktop','tool':'click','capture':cap,'arguments':{'scope':'desktop','pid':1,'window_id':2,'x':25,'y':30}})
        w.tick();self.assertEqual(s.get(identifier)['state'],'succeeded')
        self.assertEqual(next(a for t,a in b.calls if t=='probe')['points'],[[25,30]])
        self.assertNotIn('bring_to_front',[t for t,a in b.calls]);self.assertNotIn('delivery_mode',b.calls[-1][1])

    def test_taskbar_surface_is_explicit_and_cannot_widen_other_inputs(self):
        s,b,w=self.setup_queue();self.capture(s);cap=self.desktop_capture(s)
        spec={'vm_id':VM_ID,'kind':'desktop','tool':'drag','surface':'taskbar','capture':cap,'arguments':{'scope':'desktop','pid':1,'window_id':2,'from_x':10,'from_y':90,'to_x':70,'to_y':90}}
        identifier=s.submit(spec);w.tick();self.assertEqual(s.get(identifier)['state'],'succeeded')
        self.assertTrue(next(a for t,a in b.calls if t=='probe')['taskbar'])
        for changes in ({'tool':'click'},{'surface':'any_window'}):
            with self.assertRaises(ValueError):s.submit({**spec,**changes})
    def test_failed_mouse_hit_never_dispatches(self):
        s,b,w=self.setup_queue();cap=self.capture(s);b.hit=False
        action=s.submit({'vm_id':VM_ID,'kind':'desktop','tool':'click','capture':cap,'arguments':{'pid':1,'window_id':2,'x':10,'y':10}})
        w.tick();self.assertEqual(s.get(action)['state'],'refused');self.assertNotIn('click',[t for t,a in b.calls])
    def test_mouse_input_and_explicit_foreground_delivery_are_independent(self):
        s,b,w=self.setup_queue();cap=self.capture(s)
        action=s.submit({'vm_id':VM_ID,'kind':'desktop','tool':'drag','capture':cap,'delivery':{'mode':'foreground'},
                         'arguments':{'pid':1,'window_id':2,'from_x':10,'from_y':10,'to_x':70,'to_y':10}})
        w.tick();r=s.get(action)
        self.assertEqual(r['spec']['input'],{'mouse':True,'keyboard':False})
        self.assertEqual(b.calls[-1][1]['delivery_mode'],'foreground')
        self.assertNotIn('bring_to_front',[t for t,a in b.calls])
        self.assertIn('probe',[t for t,a in b.calls])
        self.assertEqual(r['result']['dispatch']['selection'],'explicit')
    def test_scroll_argument_foreground_reaches_driver_without_background_override(self):
        s,b,w=self.setup_queue();cap=self.capture(s)
        action=s.submit({'vm_id':VM_ID,'kind':'desktop','tool':'scroll','capture':cap,
                         'arguments':{'pid':1,'window_id':2,'x':50,'y':50,'direction':'down','amount':4,'delivery_mode':'foreground'}})
        w.tick();result=s.get(action)['result']
        self.assertEqual(b.calls[-1][0],'scroll')
        self.assertEqual(b.calls[-1][1]['delivery_mode'],'foreground')
        self.assertEqual(result['dispatch']['delivery_mode'],'foreground')
        self.assertEqual(result['dispatch']['selection'],'explicit')
        self.assertIn('probe',[tool for tool,args in b.calls])
    def test_conflicting_delivery_modes_are_rejected_before_queueing(self):
        s,b,w=self.setup_queue();cap=self.capture(s)
        for argument_mode,execution_mode in [('foreground','background'),('background','foreground')]:
            with self.assertRaisesRegex(ValueError,'Conflicting delivery modes'):
                s.submit({'vm_id':VM_ID,'kind':'desktop','tool':'scroll','capture':cap,'delivery':{'mode':execution_mode},
                          'arguments':{'pid':1,'window_id':2,'x':50,'y':50,'direction':'down','delivery_mode':argument_mode}})
        self.assertEqual(b.calls,[])
    def test_argument_foreground_does_not_skip_pointer_refusal(self):
        s,b,w=self.setup_queue();cap=self.capture(s);b.hit=False
        action=s.submit({'vm_id':VM_ID,'kind':'desktop','tool':'scroll','capture':cap,
                         'arguments':{'pid':1,'window_id':2,'x':50,'y':50,'direction':'down','delivery_mode':'foreground'}})
        w.tick()
        self.assertEqual(s.get(action)['state'],'refused')
        self.assertNotIn('scroll',[tool for tool,args in b.calls])
    def test_delivery_cannot_disable_keyboard_or_pointer_guards(self):
        s,b,w=self.setup_queue()
        with self.assertRaises(ValueError):s.submit({'vm_id':VM_ID,'kind':'desktop','tool':'type_text','delivery':{'mode':'background'},'arguments':{'pid':1,'window_id':2,'text':'x'}})
        cap=self.capture(s);b.hit=False
        a=s.submit({'vm_id':VM_ID,'kind':'desktop','tool':'click','capture':cap,'delivery':{'mode':'foreground'},'arguments':{'pid':1,'window_id':2,'x':10,'y':10}})
        w.tick();self.assertEqual(s.get(a)['state'],'refused');self.assertNotIn('click',[t for t,a in b.calls])
    def test_keyboard_still_requires_front_and_mixed_checks_both(self):
        s,b,w=self.setup_queue()
        action=s.submit({'vm_id':VM_ID,'kind':'desktop','tool':'type_text','arguments':{'pid':1,'window_id':2,'text':'x'}})
        w.tick();self.assertEqual([t for t,a in b.calls],['bring_to_front','type_text'])
        b.calls.clear();cap=self.capture(s)
        s.submit({'vm_id':VM_ID,'kind':'desktop','tool':'click','capture':cap,'arguments':{'pid':1,'window_id':2,'x':10,'y':10,'modifier':'ctrl'}})
        w.tick();self.assertIn('bring_to_front',[t for t,a in b.calls]);self.assertIn('probe',[t for t,a in b.calls])
    def test_missing_geometry_or_stale_token_does_not_guess(self):
        s,b,w=self.setup_queue()
        action=s.submit({'vm_id':VM_ID,'kind':'desktop','tool':'click','arguments':{'pid':1,'window_id':2,'element_token':'missing'}})
        w.tick();self.assertEqual(s.get(action)['state'],'refused');self.assertFalse(b.calls)
    def test_explicit_native_refusal_is_preserved(self):
        backend=GuestBackend.__new__(GuestBackend)
        backend.request=lambda *a,**k:{'exit_code':1,'stdout':'{"effect":"refused","code":"background_unavailable"}','stderr':'','response_file':'evidence'}
        self.assertEqual(backend.cua('drag',{})['effect'],'refused')
