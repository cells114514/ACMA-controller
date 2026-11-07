import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from acma_controller.vm_tools import VMTools


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.vm=VMTools(Path(self.temp.name))
    def test_persistent_pending_operation_blocks_repeat_after_restart(self):
        with patch('acma_controller.vm_tools.subprocess.Popen',return_value=Mock(pid=123)) as launch:
            receipt=self.vm.call('vm_start',{})
            self.assertEqual(receipt['state'],'running')
            again=VMTools(self.vm.directory)
            self.assertEqual(again.call('vm_restart',{})['state'],'refused')
            launch.assert_called_once()
            _,result=self.vm.paths(receipt['operation_id'])
            result.write_text(json.dumps({'state':'unknown'}))
            self.assertEqual(again.call('vm_start',{})['state'],'refused')
    def test_launch_failure_is_unsent(self):
        with patch('acma_controller.vm_tools.subprocess.Popen',side_effect=OSError('missing')):
            receipt=self.vm.call('vm_start',{})
        self.assertEqual(receipt['state'],'refused');self.assertFalse(receipt['dispatched'])
        self.assertEqual(self.vm.pending(),[])
    def test_wait_is_local_bounded_and_pause_interruptible(self):
        clock=[0.0]
        with patch.object(self.vm,'status',return_value={'state':'running'}),patch('acma_controller.vm_tools.time.monotonic',side_effect=lambda:clock[0]),patch('acma_controller.vm_tools.time.sleep',side_effect=lambda seconds:clock.__setitem__(0,clock[0]+seconds)):
            self.assertEqual(self.vm.wait_status('a'*32)['waited_seconds'],20)
            clock[0]=0
            self.assertTrue(self.vm.wait_status('a'*32,wait_guard=lambda:False)['wait_interrupted'])
            self.assertEqual(clock[0],0)
    def test_terminal_receipt_returns_without_sleep_or_spawn(self):
        with patch.object(self.vm,'status',return_value={'state':'succeeded'}),patch('acma_controller.vm_tools.time.sleep') as sleep:
            self.assertEqual(self.vm.wait_status('a'*32)['state'],'succeeded');sleep.assert_not_called()
    def test_invalid_ids_never_become_paths(self):
        for value in ('../secret','bad','x'*32):
            with self.assertRaises(ValueError):self.vm.paths(value)
