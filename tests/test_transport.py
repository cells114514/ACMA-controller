import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from acma_controller import transport
from acma_controller.config import current


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        c=current()
        self.config={'status':'pinned','vm_id':c.vm_id,'user':c.guest_user,'computer':c.guest_computer,
                     'user_sid':'S-1-5-21-100-200-300-1001','address':'192.168.100.10','host_address':'192.168.100.1',
                     'host_key':'ssh-ed25519 synthetic-public-key'}
        for name in ('key','known_hosts','ssh_config'):
            p=self.root/name;p.write_text('');self.config[name]=str(p)
        Path(self.config['known_hosts']).write_text(transport.ALIAS+' '+self.config['host_key'])
        self.guard=patch.object(transport,'PRIVATE',self.root);self.guard.start();self.addCleanup(self.guard.stop)
    def load(self):
        (self.root/'connection.json').write_text(json.dumps(self.config))
        return transport.load_connection()
    def test_pinned_local_binding_and_strict_ssh(self):
        self.assertEqual(self.load()['vm_id'],transport.VM_ID)
        argv=transport.ssh_argv(self.config)
        self.assertIn('StrictHostKeyChecking=yes',argv)
        self.assertEqual(argv[-1],self.config['address'])
    def test_host_loopback_and_unpinned_key_refused(self):
        for address in (self.config['host_address'],'127.0.0.1','8.8.8.8'):
            self.config['address']=address
            with self.assertRaises(ValueError):self.load()
        self.config['address']='192.168.100.10'
        Path(self.config['known_hosts']).write_text('not the pinned key')
        with self.assertRaises(ValueError):self.load()
    def test_reply_identity_and_request_id_are_mandatory(self):
        reply={'request_id':'original',**{k:self.config[k] for k in ('vm_id','computer','user_sid')}}
        transport.validate_reply(self.config,'original',reply)
        with self.assertRaises(ValueError):transport.validate_reply(self.config,'other',reply)
        with self.assertRaises(ValueError):transport.validate_reply(self.config,'original',{**reply,'computer':'HOST'})
