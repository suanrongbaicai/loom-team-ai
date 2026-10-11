"""Synthetic pre-tenant v1 authority for compatibility tests only.

Builds a historical record directly. Does not call the current capture method
and never reads site evidence, a runtime baseline, or a real image.
"""
import json
from anc.control import digest, write_durable


def capture_v1(control, ctx, root, blob, thread, turn, call):
    key = 'img-' + digest((ctx.task+'\0'+thread+'\0'+turn+'\0'+call).encode())[:40]
    path = root/ctx.task/(key+'.png')
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {'schema':'anc-native-image-v1','image':key,'task':ctx.task,
                'scope':ctx.scope,'app':'app-alice','thread':thread,'turn':turn,
                'call':call,'state':'saved','digest':digest(blob),'bytes':len(blob),
                'width':2,'height':2,'path':ctx.task+'/'+key+'.png','mime':'image/png'}
    text = json.dumps(manifest, sort_keys=True, ensure_ascii=False)
    write_durable(path, blob)
    write_durable(path.with_suffix('.json'), text.encode('utf-8'))
    control.register(ctx, key, 'data')
    control.commit(ctx, key, 0, text, 'fixture-v1-'+ctx.task, control.lease(ctx,key))
    return manifest
