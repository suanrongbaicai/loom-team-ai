"""Production wiring. Requires explicit, protected team/runtime configuration."""
import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loom.control import Control
from loom.commands import Commands
from loom.instance import InstanceLock
from loom.membership import FeishuAPI, Membership
from loom.rpc import RuntimeAPI, serve
from loom.scheduler import Scheduler
from loom.transport import LocalTransport, Sender
from loom.gateway import FeishuTransport
from loom.wechat_runner import ProtectedCollector
from loom.platform_decisions import apply_confirmed
from loom.platform_tasks import progress
from loom.images import Images
from loom.group_admission import GroupAdmission
from loom.basic_members import BasicMembers
from loom.runtime_scopes import ScopeMap


class ConfiguredAPI(RuntimeAPI):
    def __init__(self, *args, accepted_chats=None, groups=None, reactor=None, basic=None, features=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.accepted_chats = set(accepted_chats or [])
        self.groups = groups
        self.basic = basic              # same-enterprise colleagues are recognised and registered as basic members
        self.rules.features = dict(features or {})
        self.reactor = reactor          # {app: client with .react(message_id)}; a quick emoji instead of a text acknowledgement

    def accept(self, envelope):
        contact_scope = self.contacts.incoming(envelope) if self.contacts else None
        if self.groups and envelope['source']['event']['message'].get('chat_type') == 'group' and not self.groups.relevant(envelope):
            return {'ignored': 'group message not for this bot'}      # nothing read, learned, counted or stored
        if self.basic:
            self.basic.from_private_message(envelope)      # may raise Deferred: the gateway keeps the event and retries
        self.observe_unknown(envelope)   # before the deployment-chat check: a new account's first chat is not listed yet
        from loom.identity_learning import learn
        learn(self.c, envelope)       # a bound member's tenant-wide id, from their own authenticated message
        message = envelope['source']['event']['message']
        if self.groups and message.get('chat_type') == 'group':
            self.groups.ensure(envelope)     # becomes a scope only if every member is a verified roster member
        elif self.accepted_chats and message['chat_id'] not in self.accepted_chats and not contact_scope:
            # A staged controller leaves other deployed chats pending for their
            # own controller instead of turning them into permanent denials.
            raise RuntimeError('Chat outside this controller deployment')
        result = super().accept(envelope, _observed=True)
        if result.get('triggered') and not result.get('duplicate') and self.reactor:
            self.react_async(envelope)
        return result

    def react_async(self, envelope):
        client = (self.reactor or {}).get(envelope['bot_id'])
        message_id = envelope['source']['event']['message'].get('message_id')
        if client is None:
            return

        def run():
            try:
                client.react(message_id)
            except Exception as error:      # an acknowledgement emoji is optional; the work does not depend on it
                with self.c.db.transaction() as db:
                    self.c.audit(db, 'platform-verifier', None, 'reaction.failed', {'error': str(error)[:120]})
        threading.Thread(target=run, daemon=True).start()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding='utf-8-sig'))
    if config.get('schema') != 1 or not config.get('deployment_approval'):
        raise RuntimeError('Explicit approved deployment configuration required')
    root = Path(config['control_root'])
    if not root.is_absolute() or not (root / 'runtime.sqlite').is_file():
        raise RuntimeError('Provisioned protected controller state required')
    runtime = config['runtime']
    endpoint = urllib.parse.urlsplit(runtime['endpoint'])
    if endpoint.scheme != 'http' or endpoint.hostname != '127.0.0.1' or endpoint.path != '/rpc' or not endpoint.port:
        raise RuntimeError('Fixed controller loopback endpoint required')
    secret = Path(config['transport_secret_file']).read_text(encoding='utf-8-sig').strip().encode()
    if len(secret) < 32:
        raise RuntimeError('Transport credential too short')
    with InstanceLock(root / '.controller.lock'), ExitStack() as lifetime:
        c = Control(root)
        lifetime.enter_context(c.db.keep_open())
        with c.db.transaction() as db:
            policy = c.policy(db)
            if not config['scopes'] or any(s not in policy.state['scopes'] for s in config['scopes']):
                raise RuntimeError('Runtime scope has no approved policy')
        clients, transports = {}, {}
        c.secret_values = {secret.decode('utf-8', 'replace')}          # H4: exact secret values the outbound credential guard refuses to let a reply carry
        for app, item in config['apps'].items():
            credential = json.loads(Path(item['credential_file']).read_text(encoding='utf-8-sig'))
            c.secret_values.add(str(credential.get('app_secret', '')))
            if credential.get('app_id') != app:
                raise RuntimeError('Application credential binding mismatch')
            # Platform and model routes can differ. An explicit null uses
            # direct platform access without changing the model host proxy.
            clients[app] = FeishuAPI(app, credential['app_secret'], item.get('proxy', runtime.get('proxy')))
            if item.get('transport') == 'official-feishu':
                transports[app] = FeishuTransport(clients[app])
            else:
                transports[app] = LocalTransport(item['transport_endpoint'], secret)
        membership = Membership(c, clients)
        scopes = ScopeMap(config['scopes'], config.get('shared_worker_scope'), config.get('shared_worker_overrides'))
        commands = Commands(c, runtime, scopes)
        collector = (ProtectedCollector(root, runtime['framework'], config['wechat_capture'])
                     if config.get('wechat_capture', {}).get('enabled') is True else None)
        from loom.capabilities import Capabilities
        engineering=(Capabilities(c,config['capabilities'],commands,collector)
                     if config.get('capabilities',{}).get('enabled') is True else None)
        from loom.report_leaf import ReportLeaf
        from loom.feishu_reports import CloudReports
        report_config=config.get('reports',{})
        report_leaf=ReportLeaf(c,report_config.get('node'),report_config.get('resvg')) if report_config.get('enabled') is True else None
        cloud_reports=CloudReports(c,report_leaf,clients) if report_leaf else None
        if cloud_reports and report_config.get('web',{}).get('enabled') is True:
            from loom.web_reports import WebReports,Publisher
            cloud_reports.web=WebReports(cloud_reports,Publisher(report_config['web']))
            c.secret_values.add(cloud_reports.web.publisher.key)
            c.report_origin=report_config['web']['origin'].rstrip('/')       # lets the reply guard compare links with the issued origin
        from loom.public_web import PublicWeb
        public_web=PublicWeb(c,config['public_web']) if config.get('public_web',{}).get('enabled') is True else None
        basic = BasicMembers(c, directory=lambda app, open_id: clients[app].user_name(open_id))
        contacts = None
        if (config.get('private_contacts', {}).get('enabled') is True
                or config.get('private_contacts', {}).get('receive_only') is True):
            from loom.private_contacts import PrivateContacts
            contacts = PrivateContacts(c, clients, membership, config['private_contacts'])
            c.contacts = contacts
        api = ConfiguredAPI(c, runtime['framework'], secret, membership.verify, commands,
                            collector=collector,
                            images=Images(c, clients),
                            engineering=engineering,
                            report_leaf=report_leaf,cloud_reports=cloud_reports,
                            public_web=public_web,
                            contacts=contacts,
                            accepted_chats=config.get('accepted_chats'), groups=GroupAdmission(c, membership, basic), reactor=clients, basic=basic,
                            features={'group_all_messages': bool(config.get('group_all_messages')), 'on_demand_skills': config.get('on_demand_skills'),
                                      'prefetch_message': config.get('prefetch_message', {'scopes': 'all'}), 'conversation_flow': config.get('conversation_flow'), 'max_parallel': config.get('max_parallel'),
                                       'bot_identities': config.get('bot_identities')},
                            personnel_notices=config.get('personnel_notices'))
        # This gate is distinct from real-time group event subscription and introductions.
        history_config = config.get('group_history', {})
        if history_config.get('enabled') is True and history_config.get('approved') is True:
            from loom.group_history import GroupHistory
            api.group_history = GroupHistory(c, clients, history_config)
        images_config = config.get('image_generation', {})
        generated_images = None
        if images_config.get('enabled') is True and images_config.get('approved') is True:
            from loom.generated_images import GeneratedImages
            allowed = images_config.get('apps', [])
            if not allowed or any(app not in clients for app in allowed):
                raise ValueError('Approved image app binding unavailable')
            generated_images = GeneratedImages(c, allowed)
            api.generated_images = generated_images
        sender = Sender(c, transports, lambda scope: membership.verify(scope, force=True), contacts=contacts, images=generated_images)
        backends = None
        if config.get('model_backends'):
            from loom.model_backend import BackendPool
            backends = BackendPool(api, runtime, scopes, config['model_backends'])
            lifetime.callback(backends.close)
        scheduler = Scheduler(c, api, runtime, scopes, sender, backends=backends)
        scheduler.recover()
        c.recover()
        server = serve(api, endpoint.port)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            while True:
                commands.reconcile()
                scheduler.poll()
                progress(c, membership.verify)
                scheduler.send_saved()
                apply_confirmed(c, membership.verify)
                from loom.unknown_contact import resume as resume_notices
                resume_notices(c)
                scheduler.start_one()
                time.sleep(1)
        finally:
            scheduler.recover()
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    main()
