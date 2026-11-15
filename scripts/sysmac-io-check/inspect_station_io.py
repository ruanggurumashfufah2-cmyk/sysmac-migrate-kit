"""从站格式的只读预检：PDF 模块总览和旧程序行，输出人工核对资料，不生成 PLC 代码。

用法: python inspect_station_io.py <图纸.pdf> <旧程序目录> <预检输出目录>
本工具不会判断点位是否作废，不对匹配的功能、结果完整性或可运行性作保证。
"""
from pathlib import Path
import collections
import csv
import json
import re
import sys
import pymupdf

if len(sys.argv) != 4:
    sys.exit(__doc__)
pdf = Path(sys.argv[1])
old = Path(sys.argv[2])
dest = Path(sys.argv[3])
if not pdf.is_file() or not old.is_dir():
    sys.exit('图纸 PDF 或旧程序目录不存在')
dest.mkdir(exist_ok=True)
wire = re.compile(r'(?<![\w])([A-Z]+\d+)_([IQ])(\d+)_(\d{1,2})(?![\w])')
analog = re.compile(r'(?<![\w])([A-Z]+\d+)_(\d*)(A[IQ])(\d+)(?![\w])')
analog_drawing = re.compile(r'([A-Z]+\d+)_(\d*)(A[IQ])(\d+)(?:I?\+|V\+)')
title = re.compile(r'模块.*总览图')

def key(m):
    return f'{m[1]}_{m[2]}{int(m[3])}_{int(m[4])}'

def analog_key(m):
    return f'{m[1]}_{int(m[2] or 0)}{m[3]}{int(m[4])}'

drawing = collections.defaultdict(list)
other_pages = collections.defaultdict(set)
pages = []
doc = pymupdf.open(pdf)
for page_index, page in enumerate(doc):
    words = page.get_text('words')
    titles = [w[4] for w in words if title.search(w[4])]
    if not titles:
        for w in words:
            token = w[4]
            m = wire.fullmatch(token)
            a = analog_drawing.fullmatch(token) or analog.fullmatch(token)
            if m:
                other_pages[key(m)].add(page_index + 1)
            elif a:
                other_pages[analog_key(a)].add(page_index + 1)
        continue
    matches = []
    for w in words:
        m = wire.fullmatch(w[4])
        a = analog_drawing.fullmatch(w[4])
        if m:
            matches.append((w, key(m)))
        elif a:
            matches.append((w, analog_key(a)))
    if not matches:
        continue
    pages.append(page_index + 1)
    for w, normalized in matches:
        y = (w[1] + w[3]) / 2
        # Module overviews have function text in the rightmost column. Record
        # only Chinese-containing fragments; empty text remains for review.
        candidates = [x for x in words if x[0] > page.rect.width * 0.58
                      and abs((x[1] + x[3]) / 2 - y) < 5
                      and re.search(r'[\u4e00-\u9fff]', x[4])]
        candidates.sort(key=lambda x: x[0])
        function = ''.join(x[4] for x in candidates)
        drawing[normalized].append({'page': page_index + 1, 'wire': w[4], 'function': function, 'title': titles[0]})

program = collections.defaultdict(list)
for fp in sorted(old.glob('*.st.txt')):
    in_comment = False
    for ln, line in enumerate(fp.read_text(encoding='utf-8').splitlines(), 1):
        s = line.strip()
        if '(*' in s:
            in_comment = True
        active = not in_comment and not s.startswith('//')
        code = s.split('//', 1)[0] if active else ''
        if ':=' in code:
            lhs, rhs = code.split(':=', 1)
            matches = [(m, 'I') for m in wire.finditer(rhs) if m[2] == 'I']
            matches += [(m, 'Q') for m in wire.finditer(lhs) if m[2] == 'Q']
            for m, direction in matches:
                program[key(m)].append({'section': fp.name, 'line': ln, 'wire': m.group(), 'text': s})
            analog_matches = [m for m in analog.finditer(rhs) if m[3] == 'AI']
            analog_matches += [m for m in analog.finditer(lhs) if m[3] == 'AQ']
            for m in analog_matches:
                program[analog_key(m)].append({'section': fp.name, 'line': ln, 'wire': m.group(), 'text': s})
        if '*)' in s:
            in_comment = False

with (dest / '点位预检.csv').open('w', encoding='utf-8-sig', newline='') as f:
    out = csv.writer(f)
    out.writerow(['线号（标准化用于比较）', '图纸页码', '图纸线号', '图纸功能', '图纸标题', '程序段', '程序行号', '程序原文', '预检提示', '其他图纸页码线索'])
    for k in sorted(set(drawing) | set(program)):
        ds, ps = drawing.get(k, []), program.get(k, [])
        note = []
        if len(ds) > 1: note.append('图纸有多个相同线号，需核对')
        if len(ps) > 1: note.append('旧程序有多个相同线号，需核对')
        if not ds: note.append('未在已识别的模块总览页找到，需核对')
        if not ps: note.append('未在旧程序有效映射行找到，需核对')
        if ds and any(not x['function'] for x in ds): note.append('未提取到功能文字，需核对')
        for d in ds or [{}]:
            for p in ps or [{}]:
                out.writerow([k, d.get('page',''), d.get('wire',''), d.get('function',''), d.get('title',''),
                              p.get('section',''), p.get('line',''), p.get('text',''), '；'.join(note) or '仅按地址对照，功能仍需核对',
                              ', '.join(map(str, sorted(other_pages.get(k, set()))))])

summary = {'module_pages':len(pages), 'drawing_wires':len(drawing), 'drawing_rows':sum(map(len,drawing.values())),
           'old_wires':len(program), 'old_rows':sum(map(len,program.values())),
           'address_overlap':len(drawing.keys() & program.keys()), 'drawing_only':len(drawing.keys()-program.keys()),
           'program_only':len(program.keys()-drawing.keys()),
           'duplicate_drawing_wires':sum(len(v)>1 for v in drawing.values()),
           'duplicate_old_wires':sum(len(v)>1 for v in program.values()),
           'missing_function_rows':sum(not x['function'] for v in drawing.values() for x in v),
           'warning':'预检仅按线号对照；未验证语义匹配、图纸作废、缺页、模拟量或 Sysmac 编译，不能用于生成或粘贴程序。'}
(dest / '统计.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(summary, ensure_ascii=False, indent=2))
