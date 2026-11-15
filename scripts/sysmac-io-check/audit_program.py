"""清算：对生成的新程序段做全面体检，输出 清算报告.xlsx

用法: python audit_program.py <图纸点表.csv> <新程序段目录>

检查项（每项都给出线号、图纸页码、图纸功能、程序行）：
  A 变量名带奇怪后缀（_E5C06、_M0300、_新、末尾多出的编号）
  B 变量名里的编号/层/工位 与 该线号在图纸上的功能对不上
  C 同一个输入变量在多处被赋值（会互相覆盖）
  D 同一个输入线号被多个变量读取
  E 有效程序行用的线号在新图纸上不存在 / 作废
  F 图纸上有功能的点，程序里没有任何有效行
  G 变量名不合规（含 # - 括号 空格等）
"""
import csv
import os
import re
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_drawing_io import read_points_csv  # noqa: E402
import generate_program as G  # noqa: E402
from check_drawing import norm_func  # noqa: E402
from compare_io import prog_name  # noqa: E402

STATION = re.compile(r'(机头[AB]|出料[AB]|放卷|收卷)')


def keys(s):
    s = s or ''
    for a, b in [('A面涂布', '机头A'), ('B面涂布', '机头B'), ('A面牵引', '出料A'), ('B面牵引', '出料B'),
                 ('机头1#', '机头A'), ('机头2#', '机头B')]:
        s = s.replace(a, b)
    s = re.sub(r'([一二])(?=\d)', r'\1层', s)
    nums = {int(x) for x in re.findall(r'(?<![A-Za-z\d])(\d{1,2})(?:#|(?=[^\d\]]|$))', s.replace('_', ' '))
            if not re.search(r'\[' + x + r'\]', s)}
    ceng = set(re.findall(r'([一二])层', s))
    st = set(STATION.findall(s))
    return nums, ceng, st


def parse_lines(prog_dir):
    rows = []
    for fn in sorted(os.listdir(prog_dir)):
        if not fn.endswith('.st.txt'):
            continue
        sec = fn[:-7]
        lines = open(os.path.join(prog_dir, fn), encoding='utf-8').read().splitlines()
        act = G._active(lines)
        for i, (l, a) in enumerate(zip(lines, act), 1):
            if ':=' not in a:
                continue
            w = G.line_wire(a)
            if not w:
                continue
            lhs, rhs = a.split(':=', 1)
            lhs = lhs.strip()
            if w[0] in 'I' or w.startswith('AD'):
                var = lhs
            else:
                var = re.sub(r'^NOT\s+', '', rhs.strip().rstrip(';').strip())
                var = G.C.main_var(var) if hasattr(G.C, 'main_var') else var
            rows.append({'sec': sec, 'ln': i, 'line': l.strip(), 'wire': w, 'var': var,
                         'is_in': w[0] == 'I' or w.startswith('AD')})
    return rows


def main():
    csv_path, prog_dir = sys.argv[1:3]
    drawing = read_points_csv(csv_path)
    if not drawing or not any(d.get('wire') for d in drawing):
        sys.exit('图纸点表没有有效线号，已停止清算，原有报告未覆盖。')
    by_wire = {d['wire']: d for d in drawing if d['wire'] and d['void'] != '整页作废'}
    rows = parse_lines(prog_dir)
    issues = []

    def add(cat, r, why, d=None):
        d = d or by_wire.get(r['wire']) or {}
        issues.append([cat, prog_name(r['wire']), d.get('page', ''), d.get('function', ''), r['var'],
                       f"{r['sec']} 第{r['ln']}行", r['line'], why])

    for r in rows:
        v, d = r['var'], by_wire.get(r['wire'])
        # A 奇怪后缀
        if re.search(r'_[A-Z][0-9A-Z]{2,3}\d{2}$|_新$', v):
            add('A 变量名带奇怪后缀', r, '重名时自动加的后缀，需要起正式名字或核对图纸')
        # G 不合规
        if re.search(r'[#\-()（）\s]', v.split('[')[0]):
            add('G 变量名不合规', r, '含 # - 括号 或空格')
        # E 线号不在新图纸
        if d is None:
            add('E 线号不在新图纸上', r, '有效程序行用了新图纸没有/整页作废的线号')
            continue
        if d['void']:
            add('E 线号在图纸上已作废', r, '图纸该点打叉作废，程序仍在用')
        # B 编号/层/工位
        vn, vc, vs = keys(v)
        fn, fc, fs = keys(d['function'])
        bad = []
        if vn and fn and not (vn & fn):
            bad.append(f'编号 {sorted(vn)}≠图纸{sorted(fn)}')
        if vc and fc and vc != fc:
            bad.append(f'层 {"".join(vc)}层≠图纸{"".join(fc)}层')
        if vs and fs and not (vs & fs):
            bad.append(f'工位 {"".join(vs)}≠图纸{"".join(fs)}')
        if bad:
            add('B 变量名与图纸功能不一致', r, '；'.join(bad))

    # H 同一变量在多处被赋值（含非线号表达式），只看顶层有效代码
    assign = defaultdict(list)
    for fn in sorted(os.listdir(prog_dir)):
        if fn.endswith('.st.txt'):
            ls = open(os.path.join(prog_dir, fn), encoding='utf-8').read().splitlines()
            for i, (l, a, d) in enumerate(zip(ls, G._active(ls), G._depths(ls)), 1):
                m = re.match(r'^([^\s:=]+)\s*:=', a)
                if m and d == 0 and not re.match(r'(OD|DA)_', m.group(1)) and m.group(1) != '空块占位':
                    assign[m.group(1)].append((fn[:-7], i, l.strip()))
    for v, xs in assign.items():
        if len(xs) > 1:
            for sec, i, l in xs:
                issues.append(['H 同一变量多处赋值', '', '', '', v, f'{sec} 第{i}行', l,
                               f'{v} 共 {len(xs)} 处赋值，后面的会覆盖前面的'])

    # C / D
    by_var, by_w = defaultdict(list), defaultdict(list)
    for r in rows:
        if r['is_in']:
            by_var[r['var']].append(r)
            by_w[r['wire']].append(r)
    for v, rs in by_var.items():
        if len(rs) > 1 and '[' not in v or len({x['wire'] for x in rs}) > 1 and len(rs) > 1:
            for r in rs:
                add('C 同一变量多处赋值', r, f'{v} 在 {len(rs)} 处被赋值：' + '、'.join(prog_name(x["wire"]) for x in rs))
    for w, rs in by_w.items():
        if len({x['var'] for x in rs}) > 1:
            for r in rs:
                add('D 同一输入点接了多个变量', r, '、'.join(sorted({x['var'] for x in rs})))

    # F 图纸有功能、程序没有
    import json
    fb_path = os.path.join(os.path.dirname(os.path.abspath(prog_dir)), '对点反馈.json')
    fb = json.load(open(fb_path, encoding='utf-8')) if os.path.exists(fb_path) else {}
    decided = {k.split('|')[0] for k, x in fb.items()
               if re.search(r'删掉|无了|不用|注释|忽略', (x.get('judge') or '') + (x.get('remark') or ''))}
    commented = defaultdict(list)
    for fn in sorted(os.listdir(prog_dir)):
        if fn.endswith('.st.txt'):
            ls = open(os.path.join(prog_dir, fn), encoding='utf-8').read().splitlines()
            for i, (l, a) in enumerate(zip(ls, G._active(ls)), 1):
                t = l.strip()
                if ':=' in t and not a:          # // 注释行 或 (* *) 块注释里的行
                    w = G.line_wire(t.replace('(*', '').replace('*)', '').lstrip('/'))
                    if w:
                        commented[w].append((fn[:-7], i, t))
    used = {r['wire'] for r in rows}
    for w, d in by_wire.items():
        f = d['function'] or ''
        if not f or d['void'] or w in used or re.search(r'备用|占用', f) or w in decided:
            continue
        if commented.get(w):
            sec, ln, t = commented[w][0]
            m = re.match(r'[/(*\s]*([^\s:=/(*]+)\s*:=', t)
            issues.append(['F1 图纸有点，程序该线号是注释的旧行', prog_name(w), d['page'], f,
                           m.group(1) if m else '', f'{sec} 第{ln}行', t,
                           '旧变量和图纸功能对不上，需要定：启用旧行 / 换新变量 / 维持注释'])
        else:
            issues.append(['F2 图纸有点，程序完全没有', prog_name(w), d['page'], f, '', '', '',
                           '新程序里没有这个线号，需要新建变量或确认不用'])

    from openpyxl import Workbook
    from openpyxl.styles import Font
    wb = Workbook()
    ws = wb.active
    ws.title = '清算'
    ws.append(['类别', '线号', '页码', '图纸功能', '程序变量', '位置', '程序行', '说明', '你的处理'])
    for x in sorted(issues, key=lambda x: (x[0], str(x[1]))):
        ws.append(x)
    for i, wd in enumerate([22, 12, 6, 34, 36, 26, 50, 50, 20], 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = wd
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.freeze_panes = 'C2'
    ws.auto_filter.ref = ws.dimensions
    out = os.path.join(prog_dir, '清算报告.xlsx')
    wb.save(G.safe_path(out))
    from collections import Counter
    for k, n in sorted(Counter(x[0] for x in issues).items()):
        print(f'{k}: {n}')
    print('→', out)


if __name__ == '__main__':
    main()
