"""Read-only HTTP example. Start the controller service separately."""
import json
import os
from urllib.request import Request,urlopen

url='http://127.0.0.1:8767/v1/state'
request=Request(url,headers={'Authorization':'Bearer '+os.environ['ACMA_CONTROLLER_TOKEN']})
with urlopen(request,timeout=10) as response:
    print(json.dumps(json.load(response),ensure_ascii=False,indent=2))
