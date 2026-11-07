"""Authenticated loopback HTTP interface; no model and no arbitrary host shell."""
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import hashlib
import hmac
import json
import re
import threading
from urllib.parse import urlparse


def make_server(controller,token,port=8767):
    if not isinstance(token,str) or len(token)<32:raise ValueError('Use a random bearer token of at least 32 characters')
    with controller.store.db() as db:
        db.execute('CREATE TABLE IF NOT EXISTS api_calls(id TEXT PRIMARY KEY,digest TEXT NOT NULL,result TEXT)')
    journal_lock=threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass  # Never print task text or credentials.
        def reply(self,status,value):
            body=json.dumps(value,ensure_ascii=False).encode('utf-8')
            self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.end_headers()
            try:self.wfile.write(body)
            except (BrokenPipeError,ConnectionResetError):pass
        def authorized(self):
            host=self.headers.get('Host','')
            origin=self.headers.get('Origin')
            return (host in {f'127.0.0.1:{self.server.server_port}',f'localhost:{self.server.server_port}'}
                    and not origin and hmac.compare_digest(self.headers.get('Authorization','').encode('utf-8'),('Bearer '+token).encode('utf-8')))
        def do_GET(self):
            if not self.authorized():return self.reply(403,{'error':'Loopback bearer authentication required'})
            path=urlparse(self.path).path
            try:
                if path=='/v1/state':result=controller.status()
                elif path=='/v1/tools':result=controller.tools()
                elif path.startswith('/v1/operations/'):
                    identifier=path.rsplit('/',1)[-1]
                    if not re.fullmatch('[0-9a-f]{32}',identifier):raise ValueError('Invalid operation ID')
                    result=controller.status(identifier)
                elif path.startswith('/v1/captures/'):
                    identifier=path.rsplit('/',1)[-1]
                    if not re.fullmatch('[0-9a-f]{32}',identifier):raise ValueError('Invalid capture ID')
                    row=controller.store.get(identifier)
                    if row['state']!='succeeded' or not (row.get('result') or {}).get('image_path'):raise ValueError('No registered capture')
                    from pathlib import Path
                    p=Path(row['result']['image_path']).resolve()
                    if not p.is_relative_to(controller.store.directory.resolve()):raise ValueError('Unregistered capture path')
                    body=p.read_bytes();self.send_response(200);self.send_header('Content-Type','image/png')
                    self.send_header('Content-Length',str(len(body)));self.send_header('Cache-Control','no-store');self.end_headers();self.wfile.write(body);return
                else:return self.reply(404,{'error':'Unknown endpoint'})
                return self.reply(200,{'result':result})
            except (ValueError,KeyError,OSError) as error:return self.reply(409,{'error':str(error),'retry_action':False})
        def do_POST(self):
            if not self.authorized():return self.reply(403,{'error':'Loopback bearer authentication required'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=65536:raise ValueError('Expected JSON body of at most 64KiB')
                body=json.loads(self.rfile.read(length))
                if not isinstance(body,dict):raise ValueError('Expected JSON object')
                identifier=body.get('request_id')
                if not isinstance(identifier,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',identifier):raise ValueError('Stable request_id required')
                path=urlparse(self.path).path
                if path not in {'/v1/call','/v1/control'}:return self.reply(404,{'error':'Unknown endpoint'})
                allowed={'request_id','tool','arguments','capture','wait_seconds'} if path=='/v1/call' else {'request_id','command'}
                if set(body)-allowed:raise ValueError('Unexpected request fields')
                digest=hashlib.sha256((path+json.dumps(body,sort_keys=True)).encode()).hexdigest()
                with journal_lock,controller.store.db() as db:
                    row=db.execute('SELECT digest,result FROM api_calls WHERE id=?',(identifier,)).fetchone()
                    if row:
                        if row[0]!=digest:raise ValueError('request_id reused with different arguments')
                        if row[1] is None:raise ValueError('Request is pending or interrupted; inspect operations, do not replay')
                        return self.reply(200,{'request_id':identifier,'result':json.loads(row[1]),'replayed_result_only':True})
                    db.execute('INSERT INTO api_calls(id,digest) VALUES(?,?)',(identifier,digest))
                try:
                    if path=='/v1/call':
                        result=controller.call(body['tool'],body.get('arguments',{}),capture=body.get('capture'),seconds=body.get('wait_seconds',120))
                    else:result=controller.control(body['command'])
                except (ValueError,KeyError) as error:result={'error':str(error),'retry_action':False}
                except Exception as error:result={'error':str(error),'effect':'unknown','retry_action':False}
                with controller.store.db() as db:db.execute('UPDATE api_calls SET result=? WHERE id=?',(json.dumps(result),identifier))
                return self.reply(200,{'request_id':identifier,'result':result})
            except (ValueError,KeyError) as error:return self.reply(409,{'error':str(error),'retry_action':False})
    return ThreadingHTTPServer(('127.0.0.1',port),Handler)
