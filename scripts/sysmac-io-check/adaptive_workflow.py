"""Review-first workflow for station/module/channel addresses and mixed projects.

python adaptive_workflow.py <drawing.pdf> <old-program-dir> <output-dir> [--reuse]
Unknown layouts and uncertain functional matches never become executable guesses.
"""
from collections import Counter, defaultdict
from pathlib import Path
import argparse
import difflib
import json
import re
import uuid

from adaptive_drawing import extract, fingerprint
from io_address import Address, code_lines, parse_address, parse_program, load_rules, rules_signature
from review_actions import point_identity, task_id, saved_action, revision, point_task, module_task, LABELS, compact_source
from cross_match import resolve_cycles


ALIASES = [('OvenA','A面烘箱'),('OvenB','B面烘箱'),('NoseA','机头A'),('NoseB','机头B'),
           ('BottomDrv','出料A'),('UpperDrv','出料B'),('Uw','放卷'),('Rw','收卷'),
           ('A面涂布','机头A'),('B面涂布','机头B'),('A面牵引','出料A'),('B面牵引','出料B'),
           ('OS','操作侧'),('DS','驱动侧')]


def semantic(text):
    s = re.sub(r'^(DI|DQ|DO|AI|AQ|ID|OD|AD|DA)_','',text)
    for a,b in ALIASES:
        s = s.replace(a,b)
    # Array indices remain significant. Never erase device number / side / direction.
    s = re.sub(r'\[(\d+)\]',r'第\1节',s)
    s = re.sub(r'(?<!\d)0+(\d+)',r'\1',s)
    return re.sub(r'[\s_\-#（）()：:]', '', s)


def simple_var(row):
    if row['address']['kind'] in ('DI','AI'):
        return row['var']
    # Compound output expressions must remain intact; use identifiers only as suggestions.
    return row['var']


def business_name(row):
    """Compare output command names without counting its interlock expressions."""
    if row['address']['kind'] in ('DO','AO'):
        names=re.findall(r'(?<!\w)(?:DQ|DO|OD|AQ|DA)_[\w\u4e00-\u9fff]+(?:\[\d+\])?',row['var'])
        if names:return names[0]
    return row['var']


def similarity(function,row):
    a,b=semantic(function),semantic(business_name(row))
    if not a or not b:return 0.0
    return difflib.SequenceMatcher(None,a,b).ratio()


def functional_conflicts(function,row):
    """Known opposite meanings stay red regardless of textual similarity."""
    a,b=semantic(function),semantic(business_name(row))
    pairs=[(('启动',),('停止',)),(('伸出',),('缩回','收回')),
           (('抬升','抬起','升起'),('下降','落下')),
           (('打开','开启'),('关闭',)),(('操作侧',),('驱动侧','电机侧')),
           (('外侧',),('内侧',)),(('A轴',),('B轴',)),
           (('机头A',),('机头B',)),(('出料A',),('出料B',)),
           (('A面烘箱',),('B面烘箱',))]
    pairs+=[(('前进','前移'),('后退','后移')),(('放卷',),('收卷',))]
    conflicts=[]
    for left,right in pairs:
        def side(s):
            return {i for i,terms in enumerate((left,right)) if any(t in s for t in terms)}
        sa,sb=side(a),side(b)
        if len(sa)==len(sb)==1 and sa!=sb:
            conflicts.append(f'{left[0]}/{right[0]}方向或设备不一致')
    return conflicts


def feedback_for(feedback, point, row):
    var = row['var'] if row else ''
    item=feedback.get(f'{point["original_wire"]}|{point["function"]}|{var}')
    if item is not None:return item
    neutral=feedback.get(f'{point["original_wire"]}|{point["function"]}|',{})
    return neutral if neutral.get('judge')=='待判断' else {}


def compare(data, program, feedback, reviews=None, known_names=None, sections=None):
    reviews=reviews or {};known_names={n.casefold() for n in (known_names or ())}|{p['var'].casefold() for p in program};sections=set(sections or (p['section'] for p in program))
    drawing, by_key, by_old = [dict(d) for d in data['points']], defaultdict(list), defaultdict(list)
    for d in drawing:
        review=reviews.get(tuple(point_identity(d)),{})
        d['review']=review
        if review.get('error'):d['review_error']=review['error']
        if review.get('action')=='ignore':d['excluded']=True
        corrected=review.get('values',{}).get('address','').strip()
        if corrected:
            a=parse_address(corrected)
            if not a or (d['address'] and a.kind!=d['address']['kind']) or a.terminal=='-':
                d['review_error']='更正线号无效或信号类型不一致'
            else:
                d['address']=a.to_dict();d['wire']=a.program_token()
    for d in drawing:
        if d['address'] and not d.get('excluded'):
            by_key[d['address']['key']].append(d)
    for p in program:
        by_old[p['address']['key']].append(p)
    meanings=defaultdict(list)
    drawing_meanings=Counter()
    for p in program:
        a=p['address'];meanings[(a['scheme'],a['kind'],a['station'],semantic(p['var']))].append(p)
    for d in drawing:
        if d.get('excluded'):continue
        if d['address']:
            a=d['address'];drawing_meanings[(a['scheme'],a['kind'],a['station'],semantic(d['function']))]+=1
    results, questions, decisions = [], [], {}
    for d in drawing:
        if d.get('excluded'):
            results.append({**d,'status':'✅ 已排除此图纸记录','var':'','judge':LABELS['ignore'],
                            'remark':d['review'].get('remark',''),'section':'','line':'','raw':'','score':0,
                            'note':'按用户处理排除此处图纸记录，未据此改动原程序'})
            continue
        # I/O 表里没接线的空槽位：无线号、无功能文字、无地址。
        # 旧流程 compare_io.py 对这类点标「备用/作废，程序未用」直接跳过；
        # 通用识别保持同一口径，否则每页几个空通道会刷出几百条无意义的待判断。
        # 只放行三者全空的行；有线号没功能、或有功能没线号都仍要报，不能误吞图纸错误。
        if not d.get('wire') and not d.get('original_wire') and not d.get('function') \
                and not d.get('address'):
            results.append({**d,'status':'✅ 空通道/备用，程序未用','var':'','judge':'','remark':'',
                            'section':'','line':'','raw':'','score':0,
                            'note':'图纸该槽位未接线，无线号与功能文字，按备用点跳过',
                            'problem_reason':''})
            continue
        addr = d['address']
        candidates = by_old.get(addr['key'],[]) if addr else []
        p = candidates[0] if len(candidates)==1 else None
        if addr and not candidates:
            meaning=(addr['scheme'],addr['kind'],addr['station'],semantic(d['function']))
            matched=meanings.get(meaning,[])
            if len(matched)==1 and drawing_meanings[meaning]==1:
                candidate=matched[0]
                # A source physical address repeated in the old program cannot be moved automatically.
                if len(by_old[candidate['address']['key']])==1:
                    p=candidate
        fb = feedback_for(feedback,d,p)
        judge, remark = fb.get('judge','').strip(),fb.get('remark','').strip()
        reasons = []
        score = 0
        action = None
        if not addr or not d['recognized']:
            reasons.append('图纸点位的格式或功能文字不完整')
        elif len(by_key[addr['key']]) > 1:
            reasons.append('图纸地址重复：'+','.join(str(x['page']) for x in by_key[addr['key']]))
        elif len(candidates) > 1:
            reasons.append('旧程序同一地址有多处赋值')
        elif d.get('void'):
            reasons.append('图纸整页作废，需确认受影响范围')
        elif not p:
            if re.fullmatch(r'(?:备用|预留|未用|NC)',d['function'],re.I):
                action = 'unused'
            else:
                reasons.append('图纸有点，未找到同地址旧映射')
        else:
            a,b = semantic(d['function']),semantic(business_name(p))
            score = similarity(d['function'],p)
            conflicts=functional_conflicts(d['function'],p)
            if conflicts:
                reasons.extend(conflicts)
            elif a == b and a and p['simple'] and p['refs']==1:
                action = 'keep'
            elif score >= .5 and p['simple'] and p['refs']==1 and p['address']['key']==addr['key']:
                action = 'yellow'
                reasons.append('相似度≥50%，按现有地址保留原表达式，标黄供浏览')
            else:
                reasons.append('相似度不足50%或语句无法完整解析，需要判断')
        # Classification is about the underlying problem, not the last saved action.
        problem_reason='；'.join(reasons)
        # Explicit structured actions only. Free-form prose remains visible, never guessed.
        if judge in ('对','确认','保留原变量') and p and not d.get('void') and addr and d['recognized']:
            if len(candidates)==1 and p['simple'] and p['refs']==1:
                action, reasons = 'keep', []
        if judge in ('图纸多余','不用'):
            if not candidates:
                action, reasons = 'unused', []
            elif p and p['simple'] and p['refs']==1 and len(by_key[addr['key']])==1:
                action, reasons = 'comment', []
        if judge in ('待判断','错','不对'):
            action=None
            reasons.append('用户明确要求保留待判断')
        review=d.get('review',{});requested=review.get('action');values=review.get('values',{})
        comment_rows=[]
        if requested:
            error=d.get('review_error','')
            if requested=='defer':
                action=None;judge='待判断';reasons=['用户暂缓处理，继续保留待判断']
            elif not error:
                try:
                    if not addr or not d['recognized'] or d.get('void'):
                        raise ValueError('点位未完整识别或整页作废，请先核对图纸')
                    if requested=='confirm':
                        if not p or not p['simple'] or p['refs']!=1:
                            raise ValueError('没有可直接保留的唯一映射，请选择一条旧映射')
                        action='keep'
                    elif requested=='source':
                        if len(by_key[addr['key']])!=1:
                            raise ValueError('图纸地址重复，请先排除重复记录或更正线号')
                        source=values.get('source',{})
                        selected=[x for x in program if x['section']==source.get('section') and x['line']==source.get('line')]
                        if len(selected)!=1 or not selected[0]['simple'] or selected[0]['refs']!=1:
                            raise ValueError('所选行不能完整解析为单一映射，请更换旧映射')
                        if selected[0]['address']['kind']!=addr['kind']:
                            raise ValueError('所选旧映射的输入/输出或模拟量类型不一致')
                        if any(not x['simple'] or x['refs']!=1 for x in candidates):
                            raise ValueError('目标地址有复杂旧赋值，不能自动替换')
                        p=selected[0];action='keep'
                        comment_rows=[x for x in candidates if (x['section'],x['line'])!=(p['section'],p['line'])]
                    elif requested=='new':
                        name=str(values.get('var','')).strip();section=values.get('section')
                        if len(by_key[addr['key']])!=1:raise ValueError('图纸地址重复，请先处理重复记录')
                        if not re.fullmatch(r'[A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]{0,119}',name):
                            raise ValueError('新变量名只能使用字母、数字、下划线和汉字，不能含数组或空格')
                        if parse_address(name) or name.upper() in {'TRUE','FALSE','IF','THEN','ELSE','END_IF','BOOL','INT','REAL','AND','OR','NOT','CASE','FOR'}:
                            raise ValueError('新变量名不能是硬件线号或 ST 保留字')
                        if name.casefold() in known_names:
                            raise ValueError('变量已存在，请选择对应旧映射或使用新的变量名')
                        if section not in sections:raise ValueError('请选择现有 I/O 程序段')
                        if any(not x['simple'] or x['refs']!=1 for x in candidates):
                            raise ValueError('目标地址有复杂旧赋值，不能自动替换')
                        action='new';p=None;comment_rows=candidates
                    elif requested=='unused':
                        if len(by_key[addr['key']])!=1:raise ValueError('图纸地址重复，请使用“排除此处图纸记录”')
                        if any(not x['simple'] or x['refs']!=1 for x in candidates):raise ValueError('旧赋值无法完整解析，不能整行注释')
                        action='unused' if not candidates else 'comment_many';comment_rows=candidates
                    elif requested=='address':
                        if not str(values.get('address','')).strip():raise ValueError('请输入更正后的图纸线号')
                        # A corrected address is re-matched normally, not blindly approved.
                    else:
                        raise ValueError('此处理方式不适用于点位')
                    if requested not in ('address','defer'):reasons=[f'用户处理：{LABELS[requested]}']
                    judge=LABELS[requested];remark=review.get('remark','')
                except ValueError as exc:
                    error=str(exc)
            if error:
                action=None;reasons=['处理未应用：'+error];d['review_error']=error
        moved=bool(p and addr and p['address']['key']!=addr['key'])
        status = ('🔀 功能唯一对应，改线号' if moved else '✅ 已确认/功能一致') if action=='keep' else '⚠️ 名称相似，标黄保留' if action=='yellow' else '🛠 按判断注释' if action in ('comment','comment_many') else '🆕 已指定新增映射' if action=='new' else '✅ 备用或已决定不用' if action=='unused' else '❓ 待判断'
        r = {**d,'status':status,'var':p['var'] if p else '', 'judge':judge,'remark':remark,
             'section':p['section'] if p else '', 'line':p['line'] if p else '',
             'raw':p['raw'] if p else '', 'score':round(score,4),'note':'；'.join(reasons) or d.get('note',''),
             'problem_reason':problem_reason}
        if action in ('keep','yellow') and p and not moved:
            # Padding equivalence is physical-address equivalence, not a PLC
            # variable rename: mapping must use the actual unchanged source token.
            r['wire']=p['address']['raw']
        results.append(r)
        if action in ('keep','yellow'):
            decisions[(p['section'],p['line'])] = {**d,'operation':'keep','program_token':r['wire'],'comment_rows':comment_rows}
        elif action=='comment':
            decisions[(p['section'],p['line'])] = {**d,'operation':'comment'}
        elif action=='comment_many':
            for x in comment_rows:decisions[(x['section'],x['line'])]={**d,'operation':'comment'}
        elif action=='new':
            r.update(var=name,section=section,line='')
            decisions[(section,'new:'+str(point_identity(d))) ]={**d,'operation':'add','program_token':r['wire'],
                     'new_var':name,'section':section,'comment_rows':comment_rows}
        elif action is None:
            questions.append(['点位待判断',d['original_wire'],d['page'],d['function'],r['var'],r['section'],r['line'],r['note']])
    # Occupied addresses can exchange functions as a whole group. This must run
    # before the source/target guards, not as independent per-row replacements.
    resolve_cycles(results,program,decisions,by_key,by_old,semantic,business_name,functional_conflicts)
    # A single source statement must not be scheduled for two different destinations.
    assignments=defaultdict(list)
    for r in results:
        if r['status'].startswith(('✅ 已确认','🔀','⚠️')):
            assignments[(r['section'],r['line'])].append(r)
    for location,rs in assignments.items():
        if len({r['address']['key'] for r in rs})>1:
            decisions.pop(location,None)
            for r in rs:
                r['status']='❓ 待判断';r['note']='同一旧程序行对应多个目标地址，未自动修改'
                questions.append(['目标地址冲突',r['original_wire'],r['page'],r['function'],r['var'],r['section'],r['line'],r['note']])
    targets=defaultdict(list)
    for location,d in decisions.items():
        if d['operation'] in ('keep','add'):targets[d['address']['key']].append((location,d))
    for matches in targets.values():
        if len(matches)>1:
            identities={tuple(point_identity(d)) for _,d in matches}
            for location,_ in matches:decisions.pop(location,None)
            for r in results:
                if tuple(point_identity(r)) in identities:
                    r['status']='❓ 待判断';r['note']='多个处理占用同一目标地址，请排除图纸重复或撤回冲突选择'
    additions=defaultdict(list)
    for location,d in decisions.items():
        if d['operation']=='add':additions[d['new_var'].casefold()].append((location,d))
    for matches in additions.values():
        if len(matches)>1:
            identities={tuple(point_identity(d)) for _,d in matches}
            for location,_ in matches:decisions.pop(location,None)
            for r in results:
                if tuple(point_identity(r)) in identities:r['status']='❓ 待判断';r['note']='多个新映射使用相同变量名，请分别命名'
    # For displaced points, require a unique mutual best match among unused old
    # addresses within the same physical station/type; ties remain questions.
    pool = defaultdict(list)
    for p in program:
        a=p['address']
        pool[(a['scheme'],a['kind'],a['station'])].append(p)
    proposals=defaultdict(list)
    for r in results:
        if r['var'] or not r['address'] or r['status']!='❓ 待判断':
            continue
        ranked = []
        a=r['address']
        for p in pool[(a['scheme'],a['kind'],a['station'])]:
            sc = similarity(r['function'],p)
            if sc>=.5 and not functional_conflicts(r['function'],p):
                ranked.append((sc,p))
        ranked.sort(key=lambda x:-x[0])
        r['suggestions'] = [{'var':p['var'],'wire':p['address']['raw'],'score':round(sc,3)} for sc,p in ranked[:3]]
        meaning=(a['scheme'],a['kind'],a['station'],semantic(r['function']))
        if r.get('review') or not r['recognized'] or r.get('void') or len(by_key[a['key']])!=1 or drawing_meanings[meaning]!=1 or r['judge'] in ('待判断','错','不对'):
            continue
        eligible=[(sc,p) for sc,p in ranked if p['simple'] and p['refs']==1
                  and len(by_old[p['address']['key']])==1
                  and p['address']['key'] not in by_key
                  and (p['section'],p['line']) not in decisions]
        if not eligible:continue
        if len(eligible)>1 and abs(eligible[0][0]-eligible[1][0])<1e-9:
            r['note']+='；多个相同分数的候选，保留待判断'
            continue
        sc,p=eligible[0]
        proposals[(p['section'],p['line'])].append((sc,p,r))
    for location,ranked in proposals.items():
        ranked.sort(key=lambda x:-x[0])
        if len(ranked)>1 and abs(ranked[0][0]-ranked[1][0])<1e-9:
            for _,_,r in ranked:r['note']+='；多个目标竞争同一旧变量，保留待判断'
            continue
        sc,p,r=ranked[0]
        r.update(status='⚠️ 相似挪动，标黄执行',var=p['var'],section=p['section'],line=p['line'],
                 raw=p['raw'],score=round(sc,4),note='相似度≥50%，同站唯一互选，保留原表达式并按图纸改线号')
        decisions[location]={**r,'operation':'keep','program_token':r['wire']}
    # Build mandatory questions from the final tier, not from intermediate misses.
    questions=[q for q in questions if q[0]!='点位待判断']
    for r in results:
        if r.get('address'):r['duplicate_address']=len(by_key.get(r['address']['key'],[]))>1
        if r['status']=='❓ 待判断' and not any(q[0]=='目标地址冲突' and q[1]==r['original_wire'] for q in questions):
            questions.append(['点位待判断',r['original_wire'],r['page'],r['function'],r['var'],r['section'],r['line'],r['note']])
    extra = [p for p in program if p['address']['key'] not in by_key and (p['section'],p['line']) not in decisions]
    return results, questions, decisions, extra


def sheet(path,title,columns,rows):
    # Product report output follows the existing application's workbook format.
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb=Workbook(); ws=wb.active;ws.title=title;ws.append(columns)
    for row in rows:
        ws.append(row)
        if row and str(row[0]).startswith(('⚠','❓')):
            fill=PatternFill('solid',fgColor='FFF2CC' if str(row[0]).startswith('⚠') else 'FCE4D6')
            for c in ws[ws.max_row]:c.fill=fill
    if '相似度' in columns:
        col=columns.index('相似度')+1
        for line in range(2,ws.max_row+1):ws.cell(line,col).number_format='0.0%'
    for c in ws[1]:c.font=Font(bold=True);c.fill=PatternFill('solid',fgColor='E8F0FB')
    ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width=min(60,max(12,max(len(str(c.value or '')) for c in col[:100])+2))
    if '处理数据' in columns:ws.column_dimensions[ws.cell(1,columns.index('处理数据')+1).column_letter].hidden=True
    wb.save(path)


def run(pdf, old, out, reuse=False):
    old,out=Path(old),Path(out);out.mkdir(parents=True,exist_ok=True)
    rules=old.parent/'命名规则.json'
    load_rules(rules if rules.exists() else None)
    data=extract(pdf,out/'点位识别.json',reuse)
    program,unparsed=parse_program(old)
    if not program:
        raise ValueError('未识别到旧程序映射，已停止；原有程序结果未覆盖')
    feedback_path=out/'对点反馈.json'
    feedback=json.loads(feedback_path.read_text(encoding='utf-8')) if feedback_path.exists() else {}
    hashes={p.name:fingerprint(p) for p in old.glob('*.st.txt')}
    for filename in ('variables.txt','clipboard_raw.bin'):
        if (old/filename).exists():hashes[filename]=fingerprint(old/filename)
    rev=revision(data,hashes,rules_signature());generation=uuid.uuid4().hex
    reviews={tuple(point_identity(d)):saved_action(feedback,task_id(rev,'point',point_identity(d))) or {} for d in data['points']}
    declared=set()
    variables=old/'variables.txt'
    if variables.exists():declared.update(re.findall(r'N=([^\t\r\n]+)',variables.read_text(encoding='utf-8')))
    sections=sorted(p.name[:-7] for p in old.glob('*.st.txt'))
    results,questions,decisions,extra=compare(data,program,feedback,reviews,declared,sections)
    tasks=[point_task(r,program,rev,generation,feedback,sections) for r in results if r['status']=='❓ 待判断']
    unmatched_modules=defaultdict(list)
    for p in extra:
        a=p['address'];unmatched_modules[(a['scheme'],a['kind'],a['station'],a['module'])].append(p)
    forced_comments={}
    for d in decisions.values():
        for p in d.get('comment_rows',[]):forced_comments[(p['section'],p['line'])]='用户选择新映射，替换该点旧赋值'
    module_meta={}
    for identity,ps in unmatched_modules.items():
        task=module_task(identity,ps,rev,generation,feedback,sections);module_meta[identity]=task
        request=task['saved'];action=request['action'] if request else None
        if action=='keep_module':continue
        if action=='comment_module' and all(p['simple'] and p['refs']==1 for p in ps):
            for p in ps:forced_comments[(p['section'],p['line'])]='用户判断整模块停用'
            continue
        _,kind,station,module=identity
        refs=sorted({page for p in ps for page in data['references'].get(p['address']['key'],[])})
        questions.append(['旧程序模块未对应',f'{station}/{kind}/模块{module}','','',f'{len(ps)} 条映射',task['section'],task['line'],
                          '整组核对，明细见「程序有图纸无」；其他图纸页：'+','.join(map(str,refs))])
        tasks.append(task)
    prog=out/'新程序段';prog.mkdir(exist_ok=True)
    # Review drafts preserve original expressions. Any unresolved mapping blocks clipboard export.
    changes=[]
    row_index={(p['section'],p['line']):p for p in program}
    for path in old.glob('*.st.txt'):
        lines=path.read_text(encoding='utf-8').splitlines()
        for ln,raw in enumerate(lines,1):
            location=(path.name[:-7],ln)
            d=decisions.get(location);p=row_index.get(location)
            if location in forced_comments and not d:
                updated='//'+raw+'   // 新图纸注释：'+forced_comments[location]
                lines[ln-1]=updated
                changes.append([location[0],ln,'注释掉',raw,updated,forced_comments[location]])
            elif d and p and d.get('operation')=='comment':
                updated='//'+raw+'   // 新图纸注释：用户判断不用'
                lines[ln-1]=updated
                changes.append([location[0],ln,'注释掉',raw,updated,'用户判断不用'])
            elif d and p and d['address']['key']!=p['address']['key']:
                start,end=p['span'];a=Address(**{k:v for k,v in d['address'].items() if k!='key'})
                updated=raw[:start]+d['program_token']+raw[end:]
                lines[ln-1]=updated
                basis='成组错位闭环唯一配对，标黄' if d.get('automatic_swap') else '相似度≥50%唯一互选，标黄' if d.get('status','').startswith('⚠') else '功能唯一对应'
                changes.append([location[0],ln,'改线号',raw,updated,f'{basis}：PDF 第{d["page"]}页 {d["function"]}'])
        if any(c[0]==path.name[:-7] for c in changes):
            import generate_program as legacy_generator
            lines=legacy_generator.postprocess_section(lines,{'空IF注释':0,'空块占位':0})
        with (prog/path.name).open('w',encoding='utf-8',newline='\r\n') as f:
            f.write('\n'.join(lines)+'\n')
    for d in decisions.values():
        if d['operation']!='add':continue
        a=d['address'];v=d['new_var'];token=d['program_token']
        code=f'{v}:={token};' if a['kind'] in ('DI','AI') else f'{token}:={v};'
        path=prog/(d['section']+'.st.txt')
        with path.open('a',encoding='utf-8',newline='\r\n') as f:f.write(code+'   // 新增映射（用户判断）\n')
        changes.append([d['section'],'','新增','',code,f'用户指定：PDF 第{d["page"]}页 {d["function"]}'])
    newvars={}
    for d in decisions.values():
        if d.get('operation')=='comment':continue
        a=Address(**{k:v for k,v in d['address'].items() if k!='key'})
        token=d.get('program_token') or a.program_token()
        if token not in declared:
            newvars[token]=['BOOL' if a.kind in ('DI','DO') else 'INT',d['function']]
        if d['operation']=='add':newvars[d['new_var']]=['BOOL' if a.kind in ('DI','DO') else 'INT',d['function']]
    sheet(prog/'新增变量.xlsx','新增变量',['名称','数据类型','注释'],[[v,*tc] for v,tc in sorted(newvars.items())])
    with (prog/'新增变量.txt').open('w',encoding='utf-8',newline='\r\n') as f:
        f.write(''.join(f'{v}\t{t}\t\t\tFalse\tFalse\t公开\t{c}\n' for v,(t,c) in sorted(newvars.items())))
    sheet(prog/'变更清单.xlsx','变更清单',['程序段','原行号','动作','原文','新文','依据（新图纸）'],changes)
    generated_program,generated_unparsed=parse_program(prog)
    audit=[]
    for p in generated_unparsed:
        audit.append(['未完整解析旧程序','','','', '',f'{p["section"]} 第{p["line"]}行',p['raw'],p['reason'],''])
    for d in data['unrecognized_pages']:
        audit.append(['模块页未识别','',d['page'],'','','','',d['reason'],''])
    by_output=defaultdict(list)
    for p in generated_program:
        if p['address']['kind'] in ('DO','AO'):
            by_output[p['address']['key']].append(p)
    for rows in by_output.values():
        if len(rows)>1:
            for p in rows:
                audit.append(['重复输出赋值',p['address']['raw'],'','',p['var'],f'{p["section"]} 第{p["line"]}行',p['raw'],'需要确认执行条件和有效表达式',''])
    # Re-parse generated code independently. Check structure per ST block and
    # duplicate input/business assignments; source expressions are not rewritten.
    assigned=defaultdict(list)
    for path in prog.glob('*.st.txt'):
        depth=0
        source_lines=path.read_text(encoding='utf-8').splitlines()
        block_active=None
        for ln,(raw,code) in enumerate(zip(source_lines,code_lines(source_lines)),1):
            if raw.startswith('//====== ST块'):
                if block_active is False:
                    audit.append(['空 ST 块','','','','',f'{path.name} 第{ln}行','','前一块没有有效语句',''])
                block_active=False
            elif code.strip() and block_active is not None:
                block_active=True
            if raw.startswith('//====== ST块') and depth:
                audit.append(['IF 配对异常','','','','',f'{path.name} 第{ln}行','','跨 ST 块的 IF 结构','']);depth=0
            if re.match(r'\s*IF\b',code,re.I):depth+=1
            if re.match(r'\s*END_IF\b',code,re.I):depth-=1
            if depth<0:
                audit.append(['IF 配对异常','','','','',f'{path.name} 第{ln}行',raw,'缺少 IF','']);depth=0
            m=re.fullmatch(r'\s*([\w\u4e00-\u9fff]+(?:\[\d+\])?)\s*:=.*;\s*',code)
            if m and not (parse_address(m[1]) and not parse_address(m[1]).input) and m[1]!='空块占位':
                assigned[m[1]].append(f'{path.name} 第{ln}行')
        if depth:
            audit.append(['IF 配对异常','','','','',path.name,'','缺少 END_IF',''])
        if block_active is False:
            audit.append(['空 ST 块','','','','',path.name,'','末块没有有效语句',''])
    for var,locations in assigned.items():
        if len(locations)>1:
            audit.append(['多处赋值待核对','','','',var,'；'.join(locations),'','核对条件分支或覆盖关系',''])
    ready=not questions and not audit
    task_rows=[]
    for task in tasks:
        task_rows.append(['点位待判断' if task['kind']=='point' else '旧程序模块未对应',task['wire'],task['page'],task['function'],
                          task['var'],task['section'],task['line'],task['reason'],json.dumps(task,ensure_ascii=False)])
    sheet(prog/'待判断.xlsx','待判断',['类别','线号','页码','图纸功能','程序变量','程序段','行','说明','处理数据'],task_rows)
    sheet(prog/'清算报告.xlsx','清算',['类别','线号','页码','图纸功能','程序变量','位置','程序行','说明','你的处理'],audit)
    def result_meta(r):
        ident=task_id(rev,'point',point_identity(r))
        found=saved_action(feedback,ident)
        source=next((p for p in program if p['section']==r['section'] and p['line']==r['line']),None)
        return json.dumps({'review_id':ident if found else '', 'action':found,
                           'automatic_swap':r.get('automatic_swap'),
                           'source':compact_source(source) if source else None},ensure_ascii=False)
    headers=['结果','你的判断','备注','线号','图纸原线号','图纸功能','元件','图纸作废','程序变量','新程序变量','相似度','程序段','行','程序原文','说明','页码','模块型号','图纸标题','槽位','处理数据']
    report=out/'对点报告.xlsx'
    sheet(report,'对点结果',headers,[[r['status'],r['judge'],r['remark'],r['wire'],r['original_wire'],r['function'],'',r.get('void',''),r['var'],'',r['score'],r['section'],r['line'],r['raw'],r['note']+('；建议：'+json.dumps(r['suggestions'],ensure_ascii=False) if r.get('suggestions') else ''),r['page'],r.get('module',''),r['title'],'',result_meta(r)] for r in results])
    from openpyxl import load_workbook
    wb=load_workbook(report)
    ws=wb.create_sheet('图纸疑点');ws.append(['编号','类别','线号','页码','图纸功能','说明','你的结论'])
    for n,r in enumerate(results,1):
        if '重复' in r['note'] or not r['recognized'] or r.get('void'):
            ws.append([n,'图纸疑点',r['original_wire'],r['page'],r['function'],r['note'],''])
    ws=wb.create_sheet('程序有图纸无');ws.append(['线号','程序变量','是否注释','建议新线号','程序段','行','程序原文','处理数据'])
    for p in extra:
        a=p['address'];t=module_meta[(a['scheme'],a['kind'],a['station'],a['module'])]
        ws.append([a['raw'],p['var'],'用户停用' if (p['section'],p['line']) in forced_comments else '', '',p['section'],p['line'],p['raw'],
                   json.dumps({'source':compact_source(p),'review_id':t['id'] if t['saved'] else '', 'action':t['saved']},ensure_ascii=False)])
    ws=wb.create_sheet('IO映射粘贴');ws.append(['页码','模块型号','模块','通道','变量（复制这列）','变量注释（图纸功能）','说明'])
    # Display complete metadata, but no copyable values while functional review is incomplete.
    for r in results:
        a=r['address']
        if a and not r.get('excluded') and not r.get('void'):ws.append([r['page'],r.get('module',''),f'{a["station"]}_{a["kind"]}{a["module"]}',a['channel'],r['wire'] if ready else '',r['function'],'按实际通道逐项映射' if ready else '待判断完成后提供映射'])
    ws=wb.create_sheet('同义词');ws.append(['图纸叫法','程序叫法','说明'])
    for a,b in ALIASES:ws.append([a,b,'内置名称归一化；不推断物理站号对应'])
    wb.save(report)
    output_hashes={p.name:fingerprint(p) for p in prog.glob('*.st.txt')}
    output_hashes['新增变量.txt']=fingerprint(prog/'新增变量.txt')
    manifest={'engine':'adaptive-v1','state':'ready_for_compile' if ready else 'needs_review','ready':ready,
              'pdf':str(Path(pdf).resolve()),'pdf_sha256':data['pdf_sha256'],'source_hashes':hashes,
              'output_hashes':output_hashes,'feedback_sha256':fingerprint(feedback_path) if feedback_path.exists() else None,
              'rules_path':str(rules.resolve()),'rules_sha256':fingerprint(rules) if rules.exists() else None,
              'points':len(results),'pages':len({r['page'] for r in results}),
              'program_rows':len(program),'confirmed_lines':len(decisions),
              'questions':len(task_rows),'audit':len(audit),'changes':len(changes),'review_revision':rev,
              'yellow':sum(r['status'].startswith('⚠') for r in results),'yellow_threshold':0.5,
              'message':'生成及静态检查通过，请在工程副本粘贴后编译验证。' if ready else '已生成对点报告及保留旧表达式的核对稿；尚有待判断内容，禁止直接写回。'}
    (prog/'迁移状态.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    (out/'对点数据.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    print(f'自动识别：{dict(Counter(r["address"]["scheme"] for r in results if r["address"]))}')
    print(json.dumps(manifest,ensure_ascii=False,indent=2))
    return manifest


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('pdf');ap.add_argument('old');ap.add_argument('out');ap.add_argument('--reuse',action='store_true')
    args=ap.parse_args()
    run(args.pdf,args.old,args.out,args.reuse)
