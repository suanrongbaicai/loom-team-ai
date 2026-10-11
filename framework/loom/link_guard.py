"""Refuse mistyped copies of report links issued for the current scope before saving or sending.
Compare against the system's own published records, and return exact links on refusal.
URLs unrelated to issued links (such as user input or web sources) pass unchanged."""
import difflib
import json
import re
import sqlite3

URL = re.compile(r'https?://[^\s<>"\'`，。；、）)\]】]+')
WEB_REPORT = re.compile(r'/r/web-[0-9a-f]{64}(?:[/?#]|$)')
NEAR = 0.88


def trusted_links(control, ctx):
    """Exact links issued in this scope: PUBLISHED interactive pages (origin + /r/<id>, state 'published' = read back and verified), their Feishu copies and the published Feishu publications."""
    links, origin = set(), (getattr(control, 'report_origin', None) or '').rstrip('/')
    with control.db.transaction() as db:
        try:
            for ident, body in db.execute('SELECT id,body FROM web_publications WHERE scope=?', (ctx.scope,)).fetchall():
                try:
                    record = json.loads(body)
                except ValueError:
                    continue
                if record.get('state') != 'published':          # prepared / unknown: nothing has been verified yet, so the address is not a delivered link
                    continue
                if origin:
                    links.add(origin + '/r/' + ident)
                try:
                    links.add(record['manifest']['backup']['url'])
                except (KeyError, TypeError):
                    pass
        except sqlite3.Error:
            pass
        try:
            for (body,) in db.execute("SELECT body FROM cloud_publications WHERE scope=? AND state='published'", (ctx.scope,)).fetchall():
                try:
                    url = json.loads(body).get('url')
                except ValueError:
                    url = None
                if url:
                    links.add(url)
        except sqlite3.Error:
            pass
    return links


def mismatches(text, trusted, origin=None):
    """[(bad_url, nearest_trusted_or_None)] for URLs that are ours (or nearly ours) but not exactly an issued link."""
    bad = []
    origin = (origin or '').rstrip('/')
    ids = {u.rsplit('/r/', 1)[1] for u in trusted if '/r/web-' in u}
    for raw in URL.findall(text or ''):
        url = raw.rstrip('.,;:!?')
        if url in trusted:
            continue
        near = difflib.get_close_matches(url, sorted(trusted), n=1, cutoff=NEAR)
        if WEB_REPORT.search(url):
            known = url.split('/r/', 1)[1].split('?')[0].split('#')[0].rstrip('/') in ids
            if not (known and not origin):          # with no configured origin only the id can be checked
                bad.append((url, near[0] if near else None))
        elif near:
            bad.append((url, near[0]))
    return bad


def guard(control, ctx, text):
    from .policy import Denied
    if 'http' not in (text or ''):
        return
    trusted = trusted_links(control, ctx)
    found = mismatches(text, trusted, getattr(control, 'report_origin', None))
    if found:
        try:                                           # K1: make refusals countable afterwards (hashes only)
            import hashlib
            with control.db.transaction() as db:
                control.audit(db, 'platform-verifier', getattr(ctx, 'task', None), 'link.guard_refused',
                              {'count': len(found), 'urls': [hashlib.sha256(u.encode()).hexdigest()[:12] for u, _ in found]})
        except Exception:
            pass
        wrong = '；'.join(re.sub(r'//[^/@\s]+@', '//', u) for u, _ in found)        # never quote userinfo (user:password@) back
        right = '；'.join(sorted({n for _, n in found if n} or trusted)[:6]) or '（系统里没有这个范围已发布的链接）'
        raise Denied('回复里的链接与系统记录不一致（疑似抄错，已拦下，没有保存也没有发送）：%s。请原样使用系统返回的链接（逐字复制，不要重打）：%s。改正后再回复一次。' % (wrong, right))
