"""Official Feishu long connection with a protected durable admission queue."""
import argparse
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from anc.gateway import Inbox, ControllerIngress, configure_sdk_proxy, configure_sdk_websocket_proxy
from anc.instance import InstanceLock


def ingress_line(receipt, payload, received_at):
    """O10: the log line for one received event.  `received_at` MUST be taken by the caller before any parsing or storing; `platform_create_ms` is the
    platform's own creation time of the message (milliseconds) or None when absent/odd."""
    created = None
    if isinstance(payload, dict):
        event = payload.get('event')
        message = event.get('message') if isinstance(event, dict) else None
        if isinstance(message, dict):
            created = message.get('create_time')
    return json.dumps({'ingress': receipt, 'received_at': round(received_at, 3), 'platform_create_ms': created})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    if config.get('schema') != 1 or not config.get('deployment_approval'):
        raise ValueError('Approved protected gateway configuration required')
    configure_sdk_proxy(config.get('proxy'))
    dependencies = Path(config['dependencies_root'])
    if not dependencies.is_absolute() or not dependencies.is_dir() or dependencies.is_symlink():
        raise ValueError('Provisioned protected SDK directory required')
    sys.path.insert(0, str(dependencies))
    credential = json.loads(Path(config['credential_file']).read_text(encoding='utf-8-sig'))
    app = config['app_id']
    if credential['app_id'] != app:
        raise ValueError('Application credential binding mismatch')
    root = Path(config['inbox_root'])
    secret = Path(config['transport_secret_file']).read_text(encoding='utf-8-sig').strip().encode()
    with InstanceLock(root / '.gateway.lock'):
        allow_private = None
        if config.get('private_contact_registry'):
            from anc.private_contacts import admission
            allow_private = lambda tenant, account, chat: admission(config['private_contact_registry'], app, tenant, account, chat)
        inbox = Inbox(root, app, config['allowed_chats'], allow_private=allow_private)
        deliver = ControllerIngress(config['controller_ingress'], secret)
        stopping = threading.Event()
        def pump():
            while not stopping.is_set():
                try:
                    result = inbox.pump_one(deliver)
                    if result:
                        print(json.dumps(result), flush=True)
                except Exception as error:
                    print(json.dumps({'inbox_error': type(error).__name__}), flush=True)
                stopping.wait(1)
        threading.Thread(target=pump, daemon=True).start()
        import lark_oapi as lark
        configure_sdk_websocket_proxy(config.get('proxy'))
        def receive(data):
            received_at = time.time()           # first thing: before marshalling and before the durable inbox write
            payload = json.loads(lark.JSON.marshal(data))
            receipt = inbox.receive(payload)
            print(ingress_line(receipt, payload, received_at), flush=True)
        handler = lark.EventDispatcherHandler.builder('', '').register_p2_im_message_receive_v1(receive).build()
        class ObservedClient(lark.ws.Client):
            # Pinned SDK 1.7.3 has reconnect observers but no initial-connect
            # callback. Report only after its actual WebSocket handshake.
            async def _connect(self):
                try:
                    await super()._connect()
                except Exception as error:
                    print(json.dumps({'state':'connection_failed','error_type':type(error).__name__,
                                      'errno':error.errno if type(getattr(error,'errno',None)) is int else None}),flush=True)
                    raise
                if self._conn is not None:
                    print(json.dumps({'state': 'connected', 'app_id': app}), flush=True)
        client = ObservedClient(app, credential['app_secret'], event_handler=handler, log_level=lark.LogLevel.ERROR)
        client.on_reconnecting = lambda: print(json.dumps({'state': 'reconnecting'}), flush=True)
        client.on_reconnected = lambda: print(json.dumps({'state': 'reconnected'}), flush=True)
        # SDK debug/error text can include signed connection URLs and payloads.
        # Operational receipts above contain only queue identities and states.
        from lark_oapi.core.log import logger
        logger.disabled = True
        print(json.dumps({'state': 'connecting', 'app_id': app}), flush=True)
        try:
            client.start()
        finally:
            stopping.set()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'state': 'failed', 'error_type': type(error).__name__}), flush=True)
        raise SystemExit(1) from None
