"""Closed permutations of occupied I/O addresses, with auditable evidence.

Resolve whole cycles only. A missing link, an ambiguous best match, or a human
decision locks the cycle; never take one source out of an unresolved group.
"""
from collections import defaultdict
import difflib
import re


CONTEXTS=('A面烘箱','B面烘箱','机头A','机头B','出料A','出料B','放卷','收卷')
SIDES=(('操作侧',),('驱动侧','电机侧'),('外侧',),('内侧',))
MOTIONS=(('伸出',),('缩回','收回'),('前进','前移'),('后退','后移'),
         ('启动',),('停止',),('抬升','抬起','升起'),('下降','落下'),
         ('打开','开启'),('关闭',))
SIGNALS=(('按钮灯','指示灯'),('按钮',),('电磁阀',),('开关',),('反馈',),('给定','控制'))


def _features(s,groups):
    return frozenset(i for i,terms in enumerate(groups) if any(t in s for t in terms))


def _signal(s):
    # "按钮灯" is a lamp, not a button plus a lamp.
    return next((i for i,terms in enumerate(SIGNALS) if any(t in s for t in terms)),None)


def _text(text,semantic):
    s=semantic(text)
    # UwA/UwB describe the A/B shaft; retain it as an identity constraint.
    s=re.sub(r'^(放卷|收卷)([AB])(?!轴)',r'\1\2轴',s)
    context=next((c for c in CONTEXTS if s.startswith(c)),'')
    if context:s=s[len(context):]
    s=s.replace('收回','缩回').replace('电动机','电机')
    return s,context


def cross_score(function,source,semantic,business_name,conflicts):
    if conflicts(function,source):return 0.0
    a,ac=_text(function,semantic);b,bc=_text(business_name(source),semantic)
    if not a or not b or (ac and bc and ac!=bc):return 0.0
    if re.fullmatch(r'(备用|预留|未用|NC)',a,re.I) or re.fullmatch(r'(备用|预留|未用|NC)',b,re.I):return 0.0
    # Missing side/direction/number evidence is not permission to erase it.
    if _features(a,SIDES)!=_features(b,SIDES) or _features(a,MOTIONS)!=_features(b,MOTIONS):return 0.0
    if re.findall(r'\d+',a)!=re.findall(r'\d+',b):return 0.0
    axes=lambda s:frozenset(re.findall(r'[AB](?=轴)',s))
    if axes(a)!=axes(b) or _signal(a)!=_signal(b):return 0.0
    if a==b:return 1.0
    # Permit a longer equipment description (夹头电机 / 电机), not two different
    # devices that happen to share "按钮/控制/信号" and the same direction.
    def device(s):
        for terms in (*SIDES,*MOTIONS,*SIGNALS):
            for t in sorted(terms,key=len,reverse=True):s=s.replace(t,'')
        return s
    da,db=device(a),device(b)
    if min(len(da),len(db))<2 or not (da in db or db in da):return 0.0
    return difflib.SequenceMatcher(None,a,b).ratio()


def resolve_cycles(results,program,decisions,by_key,by_old,semantic,business_name,conflicts):
    """Mutate results/decisions only after a complete unique cycle is proven."""
    scope=lambda a:(a['scheme'],a['kind'],a['station'])
    location=lambda p:(p['section'],p['line'])
    targets={r['address']['key']:r for r in results if r.get('address') and len(by_key[r['address']['key']])==1
             and r['recognized'] and not r.get('void') and not r.get('excluded')}
    old={k:ps[0] for k,ps in by_old.items() if len(ps)==1}
    pools=defaultdict(list);groups=defaultdict(list)
    for p in program:pools[scope(p['address'])].append(p)
    for r in targets.values():groups[scope(r['address'])].append(r)
    # Any human choice, even an unapplied/invalid one, locks its target and source.
    locked=set()
    for r in results:
        if r.get('review') or r.get('judge'):
            if r.get('address'):locked.add(r['address']['key'])
            chosen=r.get('review',{}).get('values',{}).get('source') or {}
            for p in program:
                if location(p)==(chosen.get('section'),chosen.get('line')):locked.add(p['address']['key'])
    source_locations={location(p):p for p in old.values()}
    for loc,d in decisions.items():
        if d['operation']!='keep':continue
        p=source_locations.get(loc)
        if p and p['address']['key']!=d['address']['key']:
            locked.update((p['address']['key'],d['address']['key']))
    def eligible(key):
        r=targets.get(key);p=old.get(key)
        return bool(r and p and key not in locked and p['simple'] and p['refs']==1)
    proposals={};evidence={}
    for s,rows in groups.items():
        sources=pools[s]
        scores={(r['address']['key'],location(p)):cross_score(r['function'],p,semantic,business_name,conflicts)
                for r in rows for p in sources}
        for r in rows:
            key=r['address']['key']
            if not eligible(key):continue
            ranked=sorted(((scores[(key,location(p))],p) for p in sources),key=lambda x:-x[0])
            if not ranked or ranked[0][0]<.8:continue
            score,p=ranked[0];other=ranked[1][0] if len(ranked)>1 else 0
            source_key=p['address']['key']
            if source_key==key or not eligible(source_key) or score-other<.12-1e-9:continue
            if score-scores[(key,location(old[key]))]<.12-1e-9:continue
            reverse=sorted(((scores[(t['address']['key'],location(p))],t) for t in rows),key=lambda x:-x[0])
            runner=reverse[1][0] if len(reverse)>1 else 0
            if reverse[0][1] is not r or score-runner<.12-1e-9:continue
            proposals[key]=source_key;evidence[key]=(p,score,min(score-other,score-runner))
    applied=set()
    for start in proposals:
        path=[];key=start
        while key in proposals and key not in path and key not in applied:
            path.append(key);key=proposals[key]
        if key!=start or len(path)<2:continue
        # All members are unique and every consumed source is a member. Commit
        # together so an interchanged address is never partially reassigned.
        for member in path:decisions.pop(location(old[member]),None)
        links=[f'{old[proposals[k]]["address"]["raw"]}→{old[k]["address"]["raw"]}' for k in path]
        group=' / '.join(sorted(path))
        for member in path:
            r=targets[member];p,score,margin=evidence[member]
            swap={'group':group,'count':len(path),'source_wire':p['address']['raw'],
                  'target_wire':old[member]['address']['raw'],'score':round(score,4),'margin':round(margin,4),'links':links}
            r.update(status='⚠️ 成组错位，标黄执行',wire=swap['target_wire'],var=p['var'],section=p['section'],line=p['line'],
                     raw=p['raw'],score=round(score,4),automatic_swap=swap,
                     note=f'同站 {len(path)} 点闭环唯一配对，设备/侧别/动作及编号一致；'+
                          '；'.join(links)+'；保留原变量及完整表达式，标黄供复核')
            decisions[location(p)]={**r,'operation':'keep','program_token':r['wire']}
        applied.update(path)
    return len(applied)
