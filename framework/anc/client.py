"""Worker-side RPC client. It never opens protected files or a SQLite database."""
import json
import os
import urllib.error
import urllib.request


def call(method, args):
    token = os.environ['ANC_TASK_TOKEN']
    endpoint = os.environ['ANC_ENDPOINT']
    if not endpoint.startswith('http://127.0.0.1:') or not endpoint.endswith('/rpc'):
        raise ValueError('Invalid local controller endpoint')
    request = urllib.request.Request(endpoint,
        json.dumps({'method': method, 'args': args}, ensure_ascii=False).encode(),
        {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=25) as response:
            result = json.load(response)
    except urllib.error.HTTPError as error:
        result = json.load(error)
    if not result.get('ok'):
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result['result']
