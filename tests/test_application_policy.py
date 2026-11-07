"""Denylist changes: model boundary, queue boundary, and guest-side validation."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from acma_controller.application_policy import app_name,launch_name,load_policy,save_policy
from acma_controller.control import GuestBackend,validate,VM_ID


class ApplicationPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'policy.json'
        self.override=patch('acma_controller.application_policy.POLICY_PATH',self.path);self.override.start();self.addCleanup(self.override.stop)

    def test_empty_default_and_case_insensitive_persistence(self):
        self.assertEqual(load_policy()['blocked_apps'],[])
        save_policy(['QQ','qq.exe','WeChat.exe'])
        self.assertEqual(load_policy()['blocked_apps'],['qq.exe','wechat.exe'])
        for name in ('../QQ.exe','*','C:\\QQ.exe','',None):
            with self.assertRaises(ValueError):save_policy([name])
        self.path.write_text('{broken',encoding='utf-8')
        with self.assertRaises(ValueError):load_policy()





    def test_shell_namespace_urls_relative_paths_and_inline_command_are_not_exe_launch(self):
        for name in ('shell:AppsFolder\\id','https://example.com','doc.txt','..\\calc.exe','C:\\test\\..\\calc.exe','cmd.exe /c calc.exe','"calc.exe"'):
            with self.subTest(name=name),self.assertRaises(ValueError):launch_name(name)
        self.assertEqual(launch_name(r'C:\Program Files\Paint\mspaint.exe'),'mspaint.exe')

    def test_direct_queue_cannot_launch_blacklisted_executable(self):
        save_policy(['QQ'])
        with self.assertRaisesRegex(ValueError,'blocked'):
            validate({'vm_id':VM_ID,'kind':'desktop','tool':'launch_app','arguments':{'name':'QQ.exe'}})

    def backend(self,windows):
        b=GuestBackend.__new__(GuestBackend)
        b.request=Mock(return_value={'exit_code':0,'stdout':json.dumps({'windows':windows}),'response_file':'fake'})
        return b

    def test_backend_blocks_before_input_even_when_model_policy_is_bypassed(self):
        save_policy(['QQ']);b=self.backend([{'pid':10,'window_id':20,'app_name':'QQ.exe'}])
        result=b.cua('bring_to_front',{'pid':10,'window_id':20})
        self.assertEqual(result['effect'],'refused');self.assertFalse(result['dispatched'])
        self.assertEqual([c.args[1] for c in b.request.call_args_list],['list_windows'])

    def test_nonblocked_and_missing_targets_with_active_denylist(self):
        save_policy(['QQ']);b=self.backend([{'pid':10,'window_id':20,'app_name':'mspaint.exe'}])
        b.cua('bring_to_front',{'pid':10,'window_id':20})
        self.assertEqual([c.args[1] for c in b.request.call_args_list],['list_windows','bring_to_front'])
        b=self.backend([]);self.assertFalse(b.cua('click',{'pid':10,'window_id':20})['dispatched'])


if __name__=='__main__':unittest.main()
