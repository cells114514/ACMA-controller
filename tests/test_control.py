import sys
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from acma_controller.control import Store, Worker, GuestBackend, VM_ID, validate


class Backend:
    def __init__(self):
        self.calls = []

    def cua(self, tool, args):
        self.calls.append((tool, args))
        if tool == 'bring_to_front':
            return {'landed_on_target': True, 'now_fg_hwnd': hex(args['window_id'])}
        if tool == 'click':
            self.last_click = args
            return {'effect': 'confirmed'}
        if tool == 'get_window_state':
            return {'pid':args.get('pid'),'window_id':args.get('window_id'),
                    'elements': [{'role': 'Edit', 'label': '测试区', 'value': self.user_state,
                                  'element_token': 'fresh-token','frame':{'x':10,'y':20,'w':100,'h':50}}]}
        return {'effect': 'unverifiable'}

    def pointer_probe(self,args,source):
        self.pointer_source=source
        return {'confirmed':True,'reason':'unit fake hit test'}


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.backend = Backend()
        self.worker = Worker(self.store, self.backend)

    def desktop(self):
        return {'vm_id': VM_ID, 'kind': 'desktop', 'tool': 'type_text',
                'arguments': {'pid': 100, 'window_id': 200, 'text': 'abc'}}

    def test_invalid_target_and_host_shell_rejected(self):
        for spec in [dict(self.desktop(), vm_id='host'), dict(self.desktop(), tool='shell'),
                     {'vm_id': VM_ID, 'kind': 'shell', 'script': 'whoami'}]:
            with self.assertRaises(ValueError):
                self.store.submit(spec)
        self.assertEqual(self.backend.calls, [])

    def test_pause_rejects_new_input_and_stops_previously_queued_input(self):
        self.store.set_mode('running')
        action = self.store.submit(self.desktop())
        self.store.control('pause_requested')
        with self.assertRaises(ValueError):
            self.store.submit(self.desktop())
        self.worker.tick()
        self.assertEqual(self.store.mode(), 'paused')
        self.assertEqual(self.store.get(action)['state'], 'queued')
        self.assertEqual(self.backend.calls, [])

    def test_resume_observes_guest_before_releasing_queue(self):
        self.store.control('resume_requested')
        self.worker.tick()
        self.assertEqual([x[0] for x in self.backend.calls], ['get_screen_size', 'list_apps'])
        self.assertEqual(self.store.mode(), 'running')

    def test_host_control_resume_does_not_require_powered_on_guest(self):
        self.worker.host_control=True
        def offline():raise RuntimeError('guest is Off')
        self.backend.prepare_transport=offline
        self.store.control('resume_requested');self.worker.tick()
        self.assertEqual(self.store.mode(),'running')
        self.assertEqual(self.backend.calls,[])

    def test_offline_guest_operation_refused_before_input_without_closing_host_gate(self):
        self.worker.host_control=True;self.store.set_mode('running')
        def offline():raise RuntimeError('guest is Off')
        self.backend.prepare_transport=offline
        action=self.store.submit(self.desktop());self.worker.tick()
        row=self.store.get(action)
        self.assertEqual(row['state'],'refused');self.assertFalse(row['result']['dispatched'])
        self.assertEqual(self.store.mode(),'running');self.assertEqual(self.backend.calls,[])

    def test_guest_handshake_repeats_after_lifecycle_generation_changes(self):
        self.worker.host_control=True;self.store.set_mode('running')
        probes=[];self.backend.prepare_transport=lambda:probes.append('prepare')
        spec={'vm_id':VM_ID,'kind':'observe','tool':'list_apps','arguments':{}}
        for _ in range(2):self.store.submit(spec);self.worker.tick()
        self.assertEqual(probes,['prepare'])
        with self.store.db() as db:
            db.execute("INSERT INTO settings(key,value) VALUES('guest_transport_generation','after-restart')")
        self.store.submit(spec);self.worker.tick()
        self.assertEqual(probes,['prepare','prepare'])

    def test_pause_during_lazy_handshake_prevents_input(self):
        self.worker.host_control=True;self.store.set_mode('running')
        self.backend.prepare_transport=lambda:self.store.control('pause_requested')
        action=self.store.submit(self.desktop());self.worker.tick()
        self.assertEqual(self.store.get(action)['state'],'cancelled')
        self.assertNotIn('type_text',[tool for tool,_ in self.backend.calls])

    def test_resume_refreshes_transport_before_desktop_handshake(self):
        order = []
        self.backend.prepare_transport = lambda: order.append('network') or {'status': 'rebound'}
        self.backend.cua = lambda tool, args: order.append(tool) or {}
        self.store.control('resume_requested')
        self.worker.tick()
        self.assertEqual(order, ['network', 'get_screen_size', 'list_apps'])
        self.assertEqual(self.store.mode(), 'running')

    def test_network_discovery_failure_does_not_release_queued_input(self):
        self.store.set_mode('running')
        identifier = self.store.submit(self.desktop())
        self.store.control('resume_requested')
        def failed(): raise ValueError('Cannot authenticate pinned guest')
        self.backend.prepare_transport = failed
        self.worker.tick()
        self.assertEqual(self.store.mode(), 'unknown')
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(self.store.get(identifier)['state'], 'queued')

    def test_startup_network_failure_pauses_without_touching_task(self):
        self.store.set_mode('running')
        identifier = self.store.submit(self.desktop())
        def failed(): raise ValueError('Guest network task not installed')
        self.backend.prepare_transport = failed
        self.worker.shutdown.set()
        with patch.object(self.worker, 'acquire'), patch.object(self.worker, 'close'):
            self.worker.run()
        self.assertEqual(self.store.mode(), 'paused')
        self.assertEqual(self.store.get(identifier)['state'], 'queued')
        self.assertEqual(self.backend.calls, [])

    def test_resume_observation_error_enters_unknown_and_keeps_writes_queued(self):
        self.store.set_mode('running')
        queued = self.store.submit(self.desktop())
        self.store.control('pause_requested')
        self.worker.tick()
        self.store.control('resume_requested')
        original = self.backend.cua
        def unavailable(tool, args):
            if tool == 'list_apps':
                return {'error': 'guest desktop unavailable'}
            return original(tool, args)
        self.backend.cua = unavailable
        self.assertFalse(self.worker.tick())
        self.assertEqual(self.store.mode(), 'unknown')
        self.assertEqual(self.store.get(queued)['state'], 'queued')
        with self.assertRaisesRegex(ValueError, 'unknown'):
            self.store.submit(self.desktop())
        with self.store.db() as db:
            event = db.execute("SELECT kind,detail FROM events ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(event['kind'], 'resume_failed')
        self.assertIn('guest desktop unavailable', event['detail'])

    def test_resume_transport_exception_enters_unknown_and_can_be_explicitly_retried(self):
        self.store.set_mode('unknown')
        self.store.control('resume_requested')
        def unavailable(tool, args):
            if tool == 'get_screen_size':
                raise OSError('SSH timeout')
            return {'effect': 'unverifiable'}
        self.backend.cua = unavailable
        self.assertFalse(self.worker.tick())
        self.assertEqual(self.store.mode(), 'unknown')
        self.store.control('resume_requested')
        self.backend.cua = lambda tool, args: {'width': 1024, 'height': 768} if tool == 'get_screen_size' else {'apps': []}
        self.assertFalse(self.worker.tick())
        self.assertEqual(self.store.mode(), 'running')

    def test_unknown_action_still_blocks_explicit_resume(self):
        self.store.set_mode('running')
        action = self.store.submit(self.desktop())
        self.store.finish(action, 'unknown', {'error': 'dispatch response lost'})
        with self.assertRaisesRegex(ValueError, 'reconciled'):
            self.store.control('resume_requested')

    def test_failed_desktop_dispatch_becomes_unknown_without_retry(self):
        self.store.set_mode('running')
        action = self.store.submit(self.desktop())
        self.backend.cua = lambda tool, args: (_ for _ in ()).throw(OSError('SSH disconnected'))
        self.worker.tick()
        self.assertEqual(self.store.get(action)['state'], 'unknown')
        self.assertEqual(self.store.mode(), 'unknown')
        calls = []
        self.backend.cua = lambda tool, args: calls.append((tool, args)) or {'effect': 'confirmed'}
        self.worker.tick()
        self.assertEqual(calls, [])

    def test_manual_handoff_pause_observe_then_resume_uses_fresh_state(self):
        self.store.set_mode('running')
        self.backend.user_state = 'before user handoff'
        self.store.control('pause_requested')
        self.worker.tick()
        self.assertEqual(self.store.mode(), 'paused')
        with self.assertRaisesRegex(ValueError, 'paused'):
            self.store.submit(self.desktop())

        # Represents a human edit made outside the controller while paused.
        self.backend.user_state = 'human changed the test page'
        observed = self.store.submit({'vm_id': VM_ID, 'kind': 'observe', 'tool': 'get_window_state',
                                      'arguments': {'pid': 100, 'window_id': 200}})
        self.worker.tick()
        self.assertEqual(self.store.get(observed)['result']['elements'][0]['value'], 'human changed the test page')

        self.store.control('resume_requested')
        self.worker.tick()
        self.assertEqual(self.store.mode(), 'running')
        spec = self.desktop()
        spec['tool'] = 'click'
        spec['arguments'] = {'pid': 100, 'window_id': 200, 'element_token': 'fresh-token',
                             'session': 'handoff-test'}
        action = self.store.submit(spec)
        self.worker.tick()
        self.assertEqual(self.store.get(action)['state'], 'succeeded')
        self.assertEqual(self.backend.last_click['element_token'], 'fresh-token')


    def test_stop_cancels_waiting_actions_without_dispatch(self):
        self.store.set_mode('running')
        action = self.store.submit(self.desktop())
        self.store.control('stop_requested')
        self.worker.tick()
        self.assertEqual(self.store.get(action)['state'], 'cancelled')
        self.assertEqual(self.backend.calls, [])

    def test_delivery_not_mislabeled_success(self):
        self.store.set_mode('running')
        action = self.store.submit(self.desktop())
        self.worker.tick()
        self.assertEqual(self.store.get(action)['state'], 'unverified')
        self.assertEqual([x[0] for x in self.backend.calls], ['bring_to_front', 'type_text'])

    def test_crash_recovery_does_not_repeat_running_action(self):
        self.store.set_mode('running')
        action = self.store.submit(self.desktop())
        self.store.claim()
        self.assertEqual(self.store.recover(), [action])
        self.assertEqual(self.store.get(action)['state'], 'unknown')
        self.assertIsNone(self.store.claim())
        with self.assertRaises(ValueError):
            self.store.control('resume_requested')

    def test_pixel_bounds_and_capture_target_checked_before_dispatch(self):
        capture = self.store.submit({'vm_id': VM_ID, 'kind': 'observe', 'tool': 'get_window_state', 'arguments': {}})
        self.store.finish(capture, 'succeeded', {'capture_id': 'image-1', 'pid': 100, 'window_id': 200, 'screenshot_width': 100, 'screenshot_height': 50})
        self.store.set_mode('running')
        for x, y in [(100, 0), (0, 50), (-1, 1)]:
            with self.assertRaises(ValueError):
                self.store.submit({'vm_id': VM_ID, 'kind': 'desktop', 'tool': 'click', 'capture': capture,
                                   'arguments': {'pid': 100, 'window_id': 200, 'x': x, 'y': y}})
        self.assertEqual(self.backend.calls, [])

    def test_same_action_cannot_be_claimed_twice(self):
        self.store.set_mode('running')
        action = self.store.submit(self.desktop())
        found = []
        def claim():
            found.append(self.store.claim())
        threads = [threading.Thread(target=claim) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(sum(item is not None for item in found), 1)
        self.assertEqual(next(item[0] for item in found if item), action)

    def test_stop_does_not_bypass_unknown_reconciliation(self):
        self.store.set_mode('running')
        action = self.store.submit(self.desktop())
        self.store.finish(action, 'unknown', {'error': 'lost response'})
        self.store.control('stop_requested')
        self.worker.tick()
        with self.assertRaises(ValueError):
            self.store.control('resume_requested')

    def test_hotkey_uses_explicit_foreground_key_with_modifiers(self):
        self.store.set_mode('running')
        spec = self.desktop()
        spec.update(tool='hotkey', arguments={'pid': 100, 'window_id': 200, 'keys': ['ctrl', 'end']})
        action = self.store.submit(spec)
        self.worker.tick()
        tool, args = self.backend.calls[-1]
        self.assertEqual((tool, args['key'], args['modifiers'], args['delivery_mode']), ('press_key', 'end', ['ctrl'], 'foreground'))
        self.assertEqual(self.store.get(action)['result']['executed_tool'], 'press_key')

    def test_invalid_arguments_and_budget_types_rejected(self):
        for args in [{'pid': 1, 'window_id': 2, 'bogus': True}, {'pid': 1, 'window_id': 2, 'x': 1}, {'pid': 1, 'window_id': 2}]:
            with self.assertRaises(ValueError):
                validate({'vm_id': VM_ID, 'kind': 'desktop', 'tool': 'click', 'arguments': args})
        with self.assertRaises(ValueError):
            validate({'vm_id': VM_ID, 'kind': 'command', 'script': 'whoami', 'timeout_ms': '1000'})

    def test_second_worker_cannot_own_same_vm(self):
        # Exercise the real mutex API without claiming the live VM namespace.
        with patch('acma_controller.control.VM_ID', 'unit-test-' + uuid.uuid4().hex):
            self.worker.acquire()
            try:
                with tempfile.TemporaryDirectory() as other:
                    contender = Worker(Store(other), Backend())
                    with self.assertRaises(RuntimeError):
                        contender.acquire()
            finally:
                self.worker.close()

    def test_stale_capture_rejected_before_valid_pixel_dispatch(self):
        capture = self.store.submit({'vm_id': VM_ID, 'kind': 'observe', 'tool': 'get_window_state', 'arguments': {}})
        self.store.finish(capture, 'succeeded', {'capture_id': 'old', 'pid': 100, 'window_id': 200,
             'screenshot_width': 100, 'screenshot_height': 50, 'observed_at': '2020-01-01T00:00:00+00:00'})
        self.store.set_mode('running')
        with self.assertRaisesRegex(ValueError, 'expired'):
            self.store.submit({'vm_id': VM_ID, 'kind': 'desktop', 'tool': 'click', 'capture': capture,
                              'arguments': {'pid': 100, 'window_id': 200, 'x': 5, 'y': 5}})

    def test_explicit_reconciliation_preserves_error_and_does_not_resume(self):
        self.store.set_mode('running')
        action = self.store.submit(self.desktop())
        self.store.finish(action, 'unknown', {'error': 'lost response'})
        self.store.resolve(action, 'succeeded', 'Independent guest readback action proves the expected final value')
        result = self.store.get(action)
        self.assertEqual(result['result']['error'], 'lost response')
        self.assertEqual(result['result']['resolution']['previous_state'], 'unknown')
        self.assertEqual(self.store.mode(), 'paused')

    def test_terminal_command_must_report_pinned_worker_identity(self):
        from types import SimpleNamespace
        backend = GuestBackend.__new__(GuestBackend)
        backend.config = {'computer': 'GUEST', 'user_sid': 'expected-sid'}
        backend.remote = SimpleNamespace(request=lambda *args: {'command': {'state': 'succeeded', 'computer': 'HOST', 'user_sid': 'expected-sid'}})
        with self.assertRaisesRegex(ValueError, 'identity'):
            backend.request('command_status', arguments={'job_id': 'test'})


if __name__ == '__main__':
    unittest.main()
