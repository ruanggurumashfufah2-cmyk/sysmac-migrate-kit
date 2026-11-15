"""Drawing adapters: layout evidence + typed addresses, retaining original text."""
from collections import defaultdict
from pathlib import Path
import hashlib
import json
import re

from io_address import parse_address, rules_signature


def fingerprint(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda:f.read(1024*1024), b''):
            h.update(chunk)
    return h.hexdigest()


def needs_adaptive(pdf):
    """Old program naming does not determine the new drawing's naming scheme."""
    import pymupdf
    with pymupdf.open(pdf) as doc:
        for page in doc:
            words=page.get_text('words')
            if not any(re.search(r'模块.*(?:总览|接线)图',w[4]) for w in words):
                continue
            for w in words:
                a=parse_address(w[4])
                if a and a.scheme!='legacy' and a.terminal!='-':
                    return True
    return False


def station_page(page, pno, words):
    title = next((w[4] for w in words if re.search(r'模块.*(?:总览|接线)图', w[4])), '')
    candidates = [(w, parse_address(w[4])) for w in words]
    candidates = [(w,a) for w,a in candidates if a and a.scheme != 'legacy' and a.terminal != '-']
    # Field wiring references are evidence, never silently promoted to module rows.
    if not title or len(candidates) < 2:
        return [], candidates
    from extract_drawing_io import page_crossed
    crossed = page_crossed(page)
    result, groups = [], defaultdict(list)
    for w,a in candidates:
        groups[(a.kind,a.station,a.module)].append((w,a))
    for group in groups.values():
        # A lone inconsistent station label is still a row, often precisely a
        # drawing typo we must report. Determine its axis from the whole page.
        axis_group = group if len(group)>1 else candidates
        ys = [(w[1]+w[3])/2 for w,_ in axis_group]
        xs = [(w[0]+w[2])/2 for w,_ in axis_group]
        # This adapter handles row-oriented overview diagrams only.
        if max(ys)-min(ys) <= max(xs)-min(xs):
            continue
        for w,a in group:
            cy = (w[1]+w[3])/2
            near = [v for v in words if v[0] > w[2] and
                    abs((v[1]+v[3])/2-cy) <= max(3, (w[3]-w[1])*.45) and
                    re.search(r'[\u4e00-\u9fff]', v[4])]
            near.sort(key=lambda v:v[0])
            func = ''.join(v[4] for v in near)
            # Chinese function column must lie to the right, with a matched channel row.
            slot_prefix = {'DI':r'(?:DI|IN)', 'DO':r'(?:DO|OUT)', 'AI':r'(?:AI|Input)', 'AO':r'(?:AO|AQ|Output)'}[a.kind]
            labels = [v[4] for v in words if re.fullmatch(slot_prefix+r'[0-9A-F]+\+?', v[4], re.I)]
            model = next((v[4] for v in words if re.fullmatch(r'(?:MB\d+|NX|GL\d+)-[A-Z0-9-]+',v[4])), '')
            evidence = bool(labels or model)
            result.append({'address':a.to_dict(), 'wire':a.program_token(), 'original_wire':w[4],
                           'function':func, 'page':pno+1, 'title':title, 'module':model,
                           'void':'整页作废' if crossed else '', 'layout':'module-overview-rows',
                           'recognized':bool(evidence and func),
                           'note':'' if evidence and func else '功能列或通道/模块证据不足，请核对原图'})
    return result, candidates


def extract(pdf, cache=None, reuse=False):
    sha = fingerprint(pdf)
    if reuse and cache and Path(cache).exists():
        data = json.loads(Path(cache).read_text(encoding='utf-8'))
        if data.get('version') == 2 and data.get('pdf_sha256') == sha and data.get('rules_signature') == rules_signature():
            return data
    import pymupdf
    import extract_drawing_io as legacy
    rows, evidence, unknown = [], defaultdict(set), []
    with pymupdf.open(pdf) as doc:
        pages_words = [page.get_text('words') for page in doc]
        label_map = legacy.build_label_map(pages_words)
        for n,page in enumerate(doc):
            words = pages_words[n]
            station, refs = station_page(page,n,words)
            rows.extend(station)
            for w,a in refs:
                evidence[a.key].add(n+1)
            old = legacy.page_points(page,n,words) or legacy.analog_points(page,n,words,label_map)
            for d in old:
                a = parse_address(d['wire']) if d['wire'] else None
                rows.append({**d, 'address':a.to_dict() if a else None,
                             'original_wire':d['wire'], 'wire':a.program_token() if a else '',
                             'recognized':bool(a and d['function']), 'layout':'legacy-table'})
            if not station and not old and any(re.search(r'模块.*(?:总览|接线)图',w[4]) for w in words):
                unknown.append({'page':n+1,'reason':'模块图未识别到受支持的点位布局/命名'})
    data = {'version':2,'rules_signature':rules_signature(),'pdf':str(Path(pdf).resolve()),'pdf_sha256':sha,
            'points':rows,'references':{k:sorted(v) for k,v in evidence.items()},
            'unrecognized_pages':unknown}
    if not rows or not any(r['address'] for r in rows):
        raise ValueError('图纸未识别到有效点位，未写入缓存；请检查命名格式、页面布局或文字层。')
    if cache:
        Path(cache).write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    return data
