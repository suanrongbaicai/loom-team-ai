"""Optional model-chosen lookup metadata, kept in its existing Context original."""
from datetime import date
import json

from .control import identifier
from .policy import Denied
from .references import excerpt_was_read
from .retrieval import metadata, normalized


def validate(c,ctx,db,value):
    if not isinstance(value,dict):
        raise ValueError('Explicit lookup descriptor required')
    if set(value)-{'schema','name','aliases','type','related','evidence','recheck_after'}:
        raise ValueError('Unknown lookup descriptor fields')
    recheck_after=value.get('recheck_after')
    if recheck_after is not None:
        try:
            when=date.fromisoformat(recheck_after) if isinstance(recheck_after,str) and len(recheck_after)==10 else None
        except ValueError:
            when=None
        if when is None:
            raise ValueError('recheck_after must be a YYYY-MM-DD date')
        if when<today():
            raise ValueError('recheck_after cannot be in the past; check the source first, then set the next date')
    name=value.get('name');aliases=value.get('aliases',[]);kind=value.get('type','')
    related=value.get('related',[]);evidence=value.get('evidence')
    if not isinstance(name,str) or not name.strip() or len(name)>100:
        raise ValueError('Object name must be 1..100 characters')
    if not isinstance(kind,str) or len(kind)>60:
        raise ValueError('Object type must be a short model-chosen label')
    if (not isinstance(aliases,list) or len(aliases)>20 or
            any(not isinstance(a,str) or not a.strip() or len(a)>100 for a in aliases)):
        raise ValueError('At most 20 explicitly recognized aliases')
    if not isinstance(related,list) or len(related)>30:
        raise ValueError('At most 30 related record identifiers')
    if not isinstance(evidence,list) or not 1<=len(evidence)<=6:
        raise ValueError('Provide 1..6 actually read source excerpts for this correspondence')
    aliases=list(dict.fromkeys(a.strip() for a in aliases if normalized(a)!=normalized(name)))
    related=list(dict.fromkeys(identifier(k) for k in related))
    sources=[]
    policy=c._check(db,ctx,True)
    for key in related:policy.require_read(ctx,c._document(db,key)[1])
    for item in evidence:
        if not isinstance(item,dict) or set(item)!={'id','revision','quote'} or type(item.get('revision')) is not int or item['revision']<1:
            raise ValueError('Evidence requires id, positive revision and exact quote')
        key=identifier(item['id']);revision=item['revision'];excerpt=item['quote']
        row,resource=c._document(db,key);policy.require_read(ctx,resource)
        body=c.version_bytes(db,row,revision).decode()
        if not isinstance(excerpt,str) or not excerpt or len(excerpt)>8000 or body.count(excerpt)!=1:
            raise Denied('对象对应需唯一匹配的短原文，不能用猜测代替依据。')
        begin=body.index(excerpt)
        if not excerpt_was_read(db,ctx.task,key,revision,begin,begin+len(excerpt)):
            raise Denied('先实际读取对象对应的依据，再保存名称和别名。')
        sources.append({'id':key,'revision':revision,'quote':excerpt})
    item={'schema':1,'name':name.strip(),'aliases':aliases,'type':kind,'related':related,'evidence':sources}
    if recheck_after is not None:
        item['recheck_after']=recheck_after
    return item


def today():
    """The deployment machine's local date: a team's "today" for recheck dates, not the UTC date."""
    return date.today()


def recheck(item):
    """Date comparison only: whether the model-set recheck date has arrived. Never judges the content."""
    value=(item or {}).get('recheck_after')
    if not isinstance(value,str):
        return None
    try:
        due=date.fromisoformat(value)<=today()
    except ValueError:
        return None
    return {'recheck_after':value,'due':due,
            'meaning':('已到复核日期：用于会改变结果的判断前，先重新读取来源；无法核对时说明最近核对时间并标为待核。'
                       if due else '未到复核日期；易变值仍以来源为准。')}


def compose(c,ctx,text,value):
    if not isinstance(text,str):raise ValueError('Context text required')
    with c.lock,c.db.transaction() as db:
        item=validate(c,ctx,db,value)
    if metadata(text):
        close=text.find('\n---\n',4)
        if close<0:raise ValueError('Incomplete lookup header')
        text=text[close+5:].lstrip('\n')
    elif text.startswith('---\n'):
        raise ValueError('保留正文后使用lookup保存；不要同时提交另一份frontmatter。')
    # Standard Obsidian aliases plus one optional JSON-valued YAML property.
    return ('---\nloom_lookup: '+json.dumps(item,ensure_ascii=False,sort_keys=True)+'\n'
            'aliases: '+json.dumps(item['aliases'],ensure_ascii=False)+'\n---\n\n'+text)


def retain(c,ctx,key,base,text):
    """A body-only update preserves its base version's established descriptor.

    Read the stated base, not a newer version: retries remain identical and
    concurrent edits still go through the existing conflict handling.
    """
    if not base or metadata(text):
        return text
    with c.lock,c.db.transaction() as db:
        policy=c._check(db,ctx,True)
        row,resource=c._document(db,key)
        policy.require_read(ctx,resource)
        prior=c.version_bytes(db,row,base).decode('utf-8')
    if not metadata(prior):
        return text
    if text.startswith('---\n'):
        raise ValueError('更新正文时保留原查找入口；修改名称对应请明确提供lookup。')
    end=prior.index('\n---\n',4)+5
    return prior[:end]+'\n'+text
