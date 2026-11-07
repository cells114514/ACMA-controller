import argparse
import json
import os
import sys
from pathlib import Path


def main():
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description='Model-independent ACMA controller')
    parser.add_argument('--config',type=Path,required=True,help='Local machine binding JSON')
    parser.add_argument('--data-dir',type=Path,help='Private runtime directory, outside your checkout')
    parser.add_argument('--port',type=int,default=8767)
    parser.add_argument('command',choices=['serve','tools'])
    args=parser.parse_args()
    os.environ['ACMA_CONTROLLER_CONFIG']=str(args.config.resolve())
    if args.data_dir:os.environ['ACMA_CONTROLLER_HOME']=str(args.data_dir.resolve())
    from .sdk import Controller
    if args.command=='tools':
        print(json.dumps(Controller().tools(),ensure_ascii=False,indent=2));return
    token=os.environ.get('ACMA_CONTROLLER_TOKEN','')
    if len(token)<32:parser.error('Set ACMA_CONTROLLER_TOKEN to a random value of at least 32 characters')
    from .server import make_server
    with Controller() as controller:
        server=make_server(controller,token,args.port)
        print(f'ACMA controller listening on http://127.0.0.1:{server.server_port}; input paused')
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:server.server_close()


if __name__=='__main__':main()
