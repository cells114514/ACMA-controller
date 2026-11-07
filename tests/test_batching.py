import unittest
from acma_controller.batching import execute


class BatchTests(unittest.TestCase):
    def test_fresh_refs_resolve_sequentially_without_guessing(self):
        calls=[]
        def dispatch(name,args):
            calls.append((name,args))
            return {'matches':[{'element_token':'fresh:0'}]} if name=='find_elements' else {'verification':{'status':'confirmed'}}
        result=execute([{'tool':'find_elements','arguments':{'query':'field'}},
                        {'tool':'click','arguments':{'element_token':{'$result':0,'path':'/matches/0/element_token'}}}],dispatch,lambda:None)
        self.assertEqual(result['status'],'completed');self.assertEqual(calls[1][1]['element_token'],'fresh:0')

    def test_unknown_or_mismatch_stops_rest_and_never_retries(self):
        for value in ({'state':'unknown'},{'verification':{'status':'mismatch'}},{'post_readback':{'state':'refused'}}):
            calls=[]
            def dispatch(name,args):calls.append(name);return value
            result=execute([{'tool':'click','arguments':{}},{'tool':'type_text','arguments':{}}],dispatch,lambda:None)
            self.assertEqual(calls,['click']);self.assertEqual(result['steps'][1]['status'],'not_executed')

    def test_pause_before_next_step_preserves_completed_step(self):
        checks=[0];calls=[]
        def guard():
            checks[0]+=1
            if checks[0]>1:raise ValueError('paused')
        result=execute([{'tool':'get_screen_size','arguments':{}}]*3,lambda *args:calls.append(args) or {},guard)
        self.assertEqual(len(calls),1);self.assertEqual(result['completed_count'],1);self.assertEqual(result['not_executed_count'],2)

    def test_unsupported_nested_batch_is_rejected_before_any_dispatch(self):
        calls=[]
        with self.assertRaises(ValueError):execute([{'tool':'execute_batch','arguments':{}}],lambda *a:calls.append(a),lambda:None)
        self.assertEqual(calls,[])

    def test_future_reference_cannot_execute_or_replay(self):
        calls=[]
        result=execute([{'tool':'click','arguments':{'element_token':{'$result':0,'path':'/token'}}}],lambda *a:calls.append(a),lambda:None)
        self.assertFalse(result['steps'][0]['dispatched']);self.assertEqual(calls,[])

    def test_expired_batch_sends_nothing(self):
        calls=[]
        result=execute([{'tool':'click','arguments':{}}],lambda *a:calls.append(a),lambda:None,seconds=0)
        self.assertEqual(result['status'],'interrupted');self.assertEqual(calls,[])
