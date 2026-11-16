"""Stable, revision-scoped workbench judgments stored through the feedback API."""
import hashlib
import json
import re

PREFIX='@review:'
LABELS={
    'confirm':'保留当前映射', 'source':'采用选中的旧映射',
    'new':'新建变量并添加映射', 'unused':'此点不用，注释旧映射',
    'ignore':'排除此处图纸记录，不改原程序', 'address':'更正图纸线号',
    'keep_module':'整模块保留原逻辑', 'comment_module':'整模块停用，注释映射',
    'defer':'暂缓处理，继续待判断'}


def revision(data,source_hashes,rules_signature):
    payload=[data['pdf_sha256'],source_hashes,rules_signature]
    return hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def point_identity(point):
    return [point['page'],point['original_wire'],point['function'],point.get('title','')]


def task_id(rev,kind,identity):
    payload=json.dumps([rev,kind,identity],ensure_ascii=False,separators=(',',':'))
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def saved_action(feedback,identifier):
    item=feedback.get(PREFIX+identifier+'||',{})
    try:
        value=json.loads(item.get('judge',''))
    except (ValueError,TypeError):
        return None
    if not isinstance(value,dict) or value.get('action') not in LABELS:
        return None
    values=value.get('values',{})
    if not isinstance(values,dict):return None
    error=''
    for field in ('var','section','address'):
        if field in values and (not isinstance(values[field],str) or len(values[field])>250):error='处理参数格式不正确'
    if 'source' in values:
        s=values['source']
        if not isinstance(s,dict) or not isinstance(s.get('section'),str) or not isinstance(s.get('line'),int):error='旧映射参数格式不正确'
    return {'action':value['action'],'values':values if not error else {},'remark':item.get('remark',''),'error':error}


def compact_source(p, cross=False, strong=False):
    return {'section':p['section'],'line':p['line'],'var':p['var'],
            'wire':p['address']['raw'],'kind':p['address']['kind'],
            'simple':bool(p['simple'] and p['refs']==1),'raw':p['raw'],
            'cross_station':bool(cross),'strong':bool(strong)}


def problem_group(row):
    reason=row.get('problem_reason') or row.get('note','')
    if row.get('duplicate_address') or not row.get('recognized') or row.get('void') or re.search('图纸地址重复|图纸.*作废|未完整识别',reason):return 'drawing'
    if re.search('多处赋值|多条.*赋值|多个处理占用|同一旧程序行对应多个',reason):return 'duplicate'
    if re.search('方向或设备不一致|方向冲突|侧别',reason):return 'direction'
    if '备用' in row.get('var','') and not re.fullmatch('备用|预留|未用|NC',row.get('function',''),re.I):return 'spare'
    if not row.get('var') and re.search('未找到同地址|图纸有点',reason):return 'missing'
    return 'naming' if row.get('var') else 'other'


def point_task(row,program,rev,generation,feedback,sections):
    ident=task_id(rev,'point',point_identity(row))
    a=row.get('address')
    sources=[p for p in program if a and p['address']['key']==a['key']]
    options=[]
    if a and row['recognized'] and not row.get('void'):
        if len(sources)==1 and sources[0]['simple'] and sources[0]['refs']==1:
            options.append('confirm')
        if not row.get('duplicate_address'):
            options+=['source','new']
            if all(p['simple'] and p['refs']==1 for p in sources):
                options.append('unused')
        options+=['address']
    options+=['ignore','defer']
    seen={(p['section'],p['line']) for p in sources}
    cross_sources=set()
    strong_sources=set()
    for suggestion in row.get('suggestions',[]):
        for p in program:
            if p['var']==suggestion['var'] and p['address']['raw']==suggestion['wire'] and (p['section'],p['line']) not in seen:
                sources.append(p);seen.add((p['section'],p['line']))
                if suggestion.get('cross'):
                    cross_sources.add((p['section'],p['line']))
                if suggestion.get('strong'):
                    strong_sources.add((p['section'],p['line']))
                break
    return {'id':ident,'kind':'point','identity':point_identity(row),'generation':generation,
            'wire':row['original_wire'],'target_wire':row['wire'],'page':row['page'],
            'function':row['function'],'var':row['var'],'reason':row['note'],
            'problem_reason':row.get('problem_reason') or row['note'],'problem_group':problem_group(row),
            'raw':row['raw'] or '\n'.join(p['raw'] for p in sources),'section':row['section'],
            'view_section':row['section'] or (sources[0]['section'] if sources else ''),'line':row['line'],
            'options':[{'value':o,'label':LABELS[o]} for o in options],
            'sources':[compact_source(p, (p['section'],p['line']) in cross_sources,
                                      (p['section'],p['line']) in strong_sources) for p in sources], 'sections':sections,
            'signal_kind':a['kind'] if a else '',
            'station':a['station'] if a else '', 'module':a['module'] if a else '',
            'channel':a['channel'] if a else '', 'address_scheme':a['scheme'] if a else '',
            'saved':saved_action(feedback,ident)}


def module_task(identity,rows,rev,generation,feedback,sections):
    ident=task_id(rev,'module',identity)
    options=['keep_module']
    if all(p['simple'] and p['refs']==1 for p in rows):options.append('comment_module')
    options.append('defer')
    _,kind,station,module=identity
    return {'id':ident,'kind':'module','identity':list(identity),'generation':generation,
            'problem_group':'module',
            'wire':f'{station}/{kind}/模块{module}','target_wire':'','page':'',
            'function':f'{len(rows)} 条映射整组处理','var':'','reason':'旧程序模块未在新图纸总览页对应；可保留或整组停用',
            'raw':'\n'.join(p['raw'] for p in rows),'section':'；'.join(sorted({p['section'] for p in rows})),
            'line':min(p['line'] for p in rows),'options':[{'value':o,'label':LABELS[o]} for o in options],
            'sources':[compact_source(p) for p in rows], 'sections':sections,
            'saved':saved_action(feedback,ident)}
