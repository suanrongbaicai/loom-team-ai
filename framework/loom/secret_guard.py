"""Outbound credential guard (H4, from the HA7CH comparison): a model text that carries a credential is refused BEFORE anything is saved or sent.
Two checks, neither echoes the suspected secret anywhere (not in the error, not in the audit):
 1. EXACT: any secret value this controller itself holds (app secrets, transport secret, report publish key) appearing in the text, whatever its format;
 2. SHAPES: well-known credential formats (private key blocks, Bearer tokens, sk-/AKIA/ghp_/xox keys, user:password@host, `password = <token>` style assignments).
Long hex ids and our own report links are NOT credentials and are not matched.  A refusal is audited as `secret.guard_refused` (kind only)."""
import re

PATTERNS = [
    ('private_key', re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----')),
    ('bearer', re.compile(r'\bBearer\s+[A-Za-z0-9._~+/=-]{20,}')),
    ('sk_key', re.compile(r'\bsk-[A-Za-z0-9_-]{20,}')),
    ('aws_key', re.compile(r'\bAKIA[0-9A-Z]{16}\b')),
    ('github_token', re.compile(r'\bgh[pousr]_[A-Za-z0-9]{30,}')),
    ('slack_token', re.compile(r'\bxox[abprs]-[A-Za-z0-9-]{10,}')),
    ('url_password', re.compile(r'[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@]+:[^\s/@]{3,}@')),
    ('assignment', re.compile(r'(?i)\b(?:app_?secret|api_?key|secret_?key|access_?token|client_?secret|password|passwd)\s*[:=]\s*["\']?[A-Za-z0-9_\-/+=]{12,}')),
]
MIN_SECRET = 12


def kind(text, secrets=()):
    """'exact', a shape name, or None.  Never returns the matched text."""
    text = text or ''
    for value in secrets or ():
        if isinstance(value, str) and len(value) >= MIN_SECRET and value in text:
            return 'exact'
    for name, pattern in PATTERNS:
        if pattern.search(text):
            return name
    return None


def guard(control, ctx, text):
    from .policy import Denied
    found = kind(text, getattr(control, 'secret_values', ()))
    if not found:
        return
    try:
        with control.db.transaction() as db:
            control.audit(db, 'platform-verifier', getattr(ctx, 'task', None), 'secret.guard_refused', {'kind': found})
    except Exception:
        pass                                          # the refusal itself must hold even if the audit write fails
    raise Denied('回复里疑似含有密钥/口令/令牌（类型：%s），已拦下，没有保存也没有发送。请去掉这类内容再回复；如果是用户需要的凭据，说明“出于安全不在聊天里发送”并给安全的办法。' % found)
