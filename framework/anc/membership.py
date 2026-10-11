"""Trusted Feishu audience verification, independent of model interpretation.

API contract: official larksuite/oapi-sdk-python im/v1 GetChatMembers models.
Only the explicitly configured Feishu origin receives application credentials.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .policy import Denied, instant
from .transport import NoRedirect


class AudienceUnavailable(RuntimeError):
    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


class MessageRejected(AudienceUnavailable):
    """Official message response explicitly rejected submission; no retry here."""
    def __init__(self, code):
        self.code = code
        super().__init__('Feishu message rejected; code=' + str(code))


class FeishuAPI:
    ORIGIN = 'https://open.feishu.cn'

    def __init__(self, app_id, app_secret, proxy=None):
        self.app_id, self.app_secret = app_id, app_secret
        proxies = {'http': proxy, 'https': proxy} if proxy else {}
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler(proxies), NoRedirect())
        self.lock = threading.Lock()
        self._token, self._expires = None, 0

    def _request(self, path, body=None, token=None):
        if not path.startswith('/open-apis/') or '\\' in path:
            raise ValueError('Invalid fixed API path')
        headers = {'Content-Type': 'application/json; charset=utf-8'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        request = urllib.request.Request(self.ORIGIN + path,
                                         json.dumps(body).encode() if body is not None else None,
                                         headers)
        try:
            with self.http.open(request, timeout=15) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('Response too large')
            result = json.loads(raw)
        except urllib.error.HTTPError as exc:
            # Preserve only a numeric platform error, never a response body,
            # credential, URL or platform-provided permission suggestion.
            try:
                code = json.loads(exc.read(65537)).get('code')
            except (ValueError, OSError):
                code = None
            suffix = str(code) if type(code) is int else 'http-' + str(exc.code)
            if path.startswith('/open-apis/im/v1/messages?receive_id_type=') and (type(code) is int or exc.code == 429):
                raise MessageRejected(code if type(code) is int else 429) from None
            raise AudienceUnavailable('Feishu API did not succeed; code=' + suffix,
                                      retryable=exc.code == 429 or 500 <= exc.code < 600) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise AudienceUnavailable('Feishu network request unavailable', retryable=True) from None
        except Exception as exc:
            # Do not propagate response bodies, tokens or credentials to workers.
            raise AudienceUnavailable('Feishu audience request unavailable') from None
        if result.get('code') != 0:
            if path.startswith('/open-apis/im/v1/messages?receive_id_type=') and type(result.get('code')) is int:
                raise MessageRejected(result['code'])
            raise AudienceUnavailable('Feishu audience API did not succeed; code=' + str(result.get('code')))
        return result

    def member_account(self, union_id):
        if not isinstance(union_id, str) or not union_id:
            raise ValueError('Verified tenant union_id required')
        user = self._request('/open-apis/contact/v3/users/' + urllib.parse.quote(union_id, safe='') +
                             '?user_id_type=union_id', token=self.token()).get('data', {}).get('user', {})
        status = user.get('status') or {}
        if user.get('union_id') != union_id or status.get('is_resigned') or status.get('is_frozen'):
            raise AudienceUnavailable('Current application cannot resolve an active verified member')
        return user.get('open_id')

    def token(self):
        with self.lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            result = self._request('/open-apis/auth/v3/tenant_access_token/internal',
                                   {'app_id': self.app_id, 'app_secret': self.app_secret})
            token, expiry = result.get('tenant_access_token'), result.get('expire')
            if not isinstance(token, str) or not token or not isinstance(expiry, int) or expiry < 120:
                raise AudienceUnavailable('Feishu token response incomplete')
            self._token, self._expires = token, time.monotonic() + min(expiry - 60, 3600)
            return token

    def react(self, message_id, emoji='OK'):
        """Put an emoji reaction on a message (instead of a text acknowledgement).  Best effort, never raises to the caller."""
        if not isinstance(message_id, str) or not message_id.startswith('om_'):
            raise ValueError('Verified Feishu message ID required')
        self._request('/open-apis/im/v1/messages/' + urllib.parse.quote(message_id, safe='') + '/reactions',
                      {'reaction_type': {'emoji_type': emoji}}, token=self.token())

    def user_name(self, open_id):
        """Display name of one user in this application (needs the platform's basic-user-info read permission)."""
        if not isinstance(open_id, str) or not open_id.startswith('ou_'):
            raise ValueError('Verified Feishu open id required')
        data = self._request('/open-apis/contact/v3/users/' + urllib.parse.quote(open_id, safe='') + '?user_id_type=open_id',
                             token=self.token()).get('data', {})
        return (data.get('user') or {}).get('name')

    def members(self, chat, id_type='open_id'):
        if not isinstance(chat, str) or not chat.startswith('oc_'):
            raise ValueError('Verified Feishu chat ID required')
        if id_type not in ('open_id', 'union_id'):
            raise ValueError('Unsupported member id type')
        token = self.token()
        cursor, seen, members, total = '', set(), {}, None
        for _ in range(1024):
            query = {'member_id_type': id_type, 'page_size': 100}
            if cursor:
                query['page_token'] = cursor
            path = '/open-apis/im/v1/chats/' + urllib.parse.quote(chat, safe='') + '/members?'
            data = self._request(path + urllib.parse.urlencode(query), token=token).get('data', {})
            if data.get('trigger_security_conf_limit'):
                raise AudienceUnavailable('Platform returned a restricted member view')
            count = data.get('member_total')
            if not isinstance(count, int) or count < 1 or (total is not None and count != total):
                raise AudienceUnavailable('Member count changed or unavailable')
            total = count
            if not isinstance(data.get('items'), list) or not isinstance(data.get('has_more'), bool):
                raise AudienceUnavailable('Incomplete member page')
            for item in data['items']:
                account, tenant = item.get('member_id'), item.get('tenant_key')
                if item.get('member_id_type') != id_type or not account or not tenant:
                    raise AudienceUnavailable('Member identity incomplete')
                identity = (tenant, account)
                if identity in members:
                    raise AudienceUnavailable('Overlapping member pages; retry with a fresh snapshot')
                members[identity] = {'tenant': tenant, 'account': account, 'name': item.get('name')}
            if not data['has_more']:
                if len(members) != total:
                    raise AudienceUnavailable('Member snapshot is incomplete')
                return list(members.values())
            cursor = data.get('page_token')
            if not cursor or cursor in seen:
                raise AudienceUnavailable('Member pagination did not advance')
            seen.add(cursor)
        raise AudienceUnavailable('Member pagination limit exceeded')


class Membership:
    def __init__(self, control, clients, ttl_seconds=15):
        if not 1 <= ttl_seconds <= 15:
            raise ValueError('Audience freshness must be 1..15 seconds')
        self.c, self.clients, self.ttl = control, clients, ttl_seconds

    def verify(self, scope_id, force=False, _again=True):
        with self.c.lock:
            with self.c.db.transaction() as db:
                policy = self.c.policy(db)
                scope = policy.state['scopes'].get(scope_id)
                if not scope or not scope.get('transport'):
                    raise AudienceUnavailable('Verified transport scope required')
                binding = scope['transport']
                if scope['kind'] == 'private':
                    participants = scope['participants']
                    if len(participants) != 1 or not scope.get('membership_verified'):
                        raise AudienceUnavailable('Private audience binding incomplete')
                    member = policy.state['members'].get(participants[0], {})
                    return bool(member.get('active') and member.get('verified'))
                if (not force and scope.get('membership_verified') and scope.get('membership_expires_at')
                        and instant(scope['membership_expires_at']) > datetime.now(timezone.utc)):
                    return True
                apps = binding.get('apps', [])
                if not apps or apps[0] not in self.clients:
                    raise AudienceUnavailable('Audience API client unavailable')
                app = apps[0]
                expected = set(scope['participants'])
            auto = scope.get('auto_admitted') is True
            try:
                # A group the bot was simply added to is judged by the tenant-wide person id, so it does not matter which
                # of our applications each member happens to be talking to.
                actual = self.clients[app].members(binding['chat'], 'union_id' if auto else 'open_id')
                observed = set()
                for member in actual:
                    if auto:
                        observed.add(policy.bind_identity('feishu', member['tenant'], app, None, union=member['account']))
                    else:
                        observed.add(policy.bind_identity('feishu', member['tenant'], app, member['account']))
                if observed != expected:
                    if not auto:
                        raise AudienceUnavailable('Actual group audience differs from approved scope')
                    changed = True
                else:
                    changed = False
            except Exception:
                # Expire the whole context, including pending saved replies.
                state = policy.state
                if scope.get('membership_verified'):
                    scope['membership_verified'] = False
                    scope['membership_expires_at'] = datetime.now(timezone.utc).isoformat()
                    self.c.set_policy(state, 'platform audience verification failed; suspend disclosure')
                raise AudienceUnavailable('Current group audience could not be verified') from None
            if changed:
                # Every current member is a verified roster member: the approved audience simply follows the group.
                state = json.loads(json.dumps(policy.state))
                state['scopes'][scope_id]['participants'] = sorted(observed)
                state['scopes'][scope_id]['membership_verified'] = False
                self.c.set_policy(state, 'group audience followed the platform: all current members are verified roster members')
                if not _again:
                    raise AudienceUnavailable('Group audience kept changing')
                return self.verify(scope_id, force=True, _again=False)
            expires = (datetime.now(timezone.utc) + timedelta(seconds=self.ttl)).isoformat()
            with self.c.db.transaction() as db:
                current = self.c.db.get_policy(db)
                if current['epoch'] != policy.epoch:
                    raise AudienceUnavailable('Policy changed during audience verification')
                current['scopes'][scope_id].update(membership_verified=True, membership_expires_at=expires)
                db.execute('UPDATE policy SET body=? WHERE singleton=1', (json.dumps(current),))
                fingerprint = hashlib.sha256(json.dumps(sorted(observed)).encode()).hexdigest()
                self.c.audit(db, 'platform-verifier', None, 'audience.verified',
                             {'scope': scope_id, 'members': len(observed), 'fingerprint': fingerprint, 'expires': expires})
            return True
