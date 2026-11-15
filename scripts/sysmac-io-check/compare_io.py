"""按线号比对：图纸 I/O 点表 ↔ Sysmac 映射程序段。

用法: python compare_io.py <图纸点表.csv> <程序段目录(*.st.txt)> <输出.xlsx>

线号规则:
  图纸 I-A01-00  ↔ 程序 ID_A01_00 / ID_A01[0]
  图纸 O-A04-00  ↔ 程序 OD_A04_00 / OD_A04[0]
"""
import csv
import difflib
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extract_drawing_io import prog_name, read_points_csv  # noqa: E402

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

# ---------- 解析程序 ----------
WIRE_VAR = re.compile(r'\b(ID|OD)_([A-Z])(\d[0-9A-Z]|\d{3})(?:_(\d{2})|\[(\d+)\])')
ANA_VAR = re.compile(r'\b(AD|DA)_([A-Z])(\d[0-9A-Z]|\d{3})_(\d{1,2})\b')
FUNC_VAR = re.compile(r'(?<![\w#])([A-Za-z_一-鿿][\w一-鿿]*(?:\[\d+\])?)')
OTHER_IO_VAR = re.compile(r'\b[A-Z]+\d+_(?:[IQO]\d+_\d{1,2}|\d*A[IQ]\d+)\b')
KEYWORDS = {'NOT', 'AND', 'OR', 'INT', 'REAL', 'INT_TO_REAL', 'REAL_TO_INT', 'TRUE', 'FALSE'}


def norm_wire(kind, area, mod, ch):
    if kind in ('AD', 'DA'):
        return f'{kind}-{area}{mod}-{int(ch)}'
    return f"{'I' if kind == 'ID' else 'O'}-{area}{mod}-{int(ch):02d}"


def main_var(expr):
    """从表达式中取出业务变量名（去掉函数名、常量）"""
    for m in FUNC_VAR.finditer(expr):
        t = m.group(1)
        if t.upper() in KEYWORDS or re.fullmatch(r'(INT|REAL|DINT|UINT)#?', t):
            continue
        if expr[m.end():m.end() + 1] == '(':
            continue  # 函数调用
        if WIRE_VAR.fullmatch(t) or ANA_VAR.fullmatch(t):
            continue
        return t
    return expr


def parse_program(folder):
    """返回 [{wire, var, active, negated, section, line, raw}]"""
    rows = []
    for fp in sorted(glob.glob(os.path.join(folder, '*.st.txt'))):
        section = os.path.basename(fp)[:-len('.st.txt')]
        in_block = False
        for ln, raw in enumerate(open(fp, encoding='utf-8').read().splitlines(), 1):
            s = raw.strip()
            # (* ... *) 块注释
            block_line = in_block
            if '(*' in s:
                block_line = True
                if '*)' not in s.split('(*', 1)[1]:
                    in_block = True
                s = s.replace('(*', '', 1)
            if '*)' in s:
                block_line = True
                in_block = False
                s = s.replace('*)', '', 1)
            s = s.strip()
            if ':=' not in s:
                continue
            active = not s.startswith('//') and not block_line
            code = s.lstrip('/').strip()
            comment = ''
            if active and '//' in code:
                code, comment = code.split('//', 1)
            if ':=' not in code:
                continue
            lhs, rhs = code.split(':=', 1)
            lhs, rhs = lhs.strip(), rhs.split(';')[0].strip()
            # 输入: 变量 := ID_xx ; 输出: OD_xx := 变量 ; 模拟量: 变量 := f(AD_xx) / DA_xx := f(变量)
            m_l, m_r = WIRE_VAR.search(lhs), WIRE_VAR.search(rhs)
            a_l, a_r = ANA_VAR.search(lhs), ANA_VAR.search(rhs)
            if m_r and m_r.group(1) == 'ID' and not m_l:
                m, var = m_r, lhs
            elif m_l and m_l.group(1) == 'OD':
                m, var = m_l, rhs
            elif a_r and a_r.group(1) == 'AD' and not a_l:
                m, var = a_r, main_var(lhs)
            elif a_l and a_l.group(1) == 'DA':
                m, var = a_l, main_var(rhs)
            else:
                continue
            if m.re is ANA_VAR:
                wire = norm_wire(m.group(1), m.group(2), m.group(3), m.group(4))
            else:
                ch = m.group(4) if m.group(4) is not None else m.group(5)
                wire = norm_wire(m.group(1), m.group(2), m.group(3), ch)
            if not var:
                continue
            rows.append({
                'array': m.re is WIRE_VAR and m.group(5) is not None,
                'wire': wire,
                'var': var,
                'negated': bool(re.match(r'NOT\b', rhs)) if m is m_r else False,
                'active': active,
                'section': section, 'line': ln, 'raw': s,
                'comment': comment.strip() if active else s.split(';', 1)[-1].strip(),
            })
    return rows


def check_unsupported_program(folder):
    """Reject unsupported station/module/channel tokens, including mixed-format programs."""
    for fp in sorted(glob.glob(os.path.join(folder, '*.st.txt'))):
        in_block = False
        with open(fp, encoding='utf-8') as f:
            lines = f.read().splitlines()
        for ln, raw in enumerate(lines, 1):
            # Ignore comments, including multi-line ST comments; never rewrite source.
            code = ''
            pos = 0
            while pos < len(raw):
                if in_block:
                    end = raw.find('*)', pos)
                    if end < 0:
                        break
                    in_block = False
                    pos = end + 2
                else:
                    start = raw.find('(*', pos)
                    line_comment = raw.find('//', pos)
                    if line_comment >= 0 and (start < 0 or line_comment < start):
                        code += raw[pos:line_comment]
                        break
                    if start < 0:
                        code += raw[pos:]
                        break
                    code += raw[pos:start]
                    in_block = True
                    pos = start + 2
            match = OTHER_IO_VAR.search(code) if ':=' in code else None
            if match:
                raise ValueError(f'旧程序包含暂不支持的从站线号 {match.group()}（{os.path.basename(fp)} 第 {ln} 行），已停止；请先适配该格式。')


# ---------- 名称比较 ----------
SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
SYN_FILE = os.path.join(SKILL_DIR, 'synonyms.json')
DEFAULT_SYN = [
    ('A面涂布', '机头A'), ('B面涂布', '机头B'), ('A面牵引', '出料A'), ('B面牵引', '出料B'),
    ('旋塔', '转塔'), ('感应器', '传感器'), ('原位', '原点'), ('故障', '报警'),
    ('ALM', '报警'), ('电磁阀', '阀'), ('按钮', ''), ('信号', ''), ('指示灯', '灯'),
    ('变频器', '变频'), ('循环风机', '内循环'), ('一', '1'), ('二', '2'),
]


def load_synonyms():
    """同义词表：skill 目录下 synonyms.json（由 learn_feedback.py 维护），首次运行用默认值"""
    if os.path.exists(SYN_FILE):
        with open(SYN_FILE,encoding='utf-8') as f:
            syn = [tuple(x) for x in json.load(f)]
    else:
        syn = list(DEFAULT_SYN)
    return sorted(syn, key=lambda x: -len(x[0]))


SYN = load_synonyms()
STRIP = re.compile(r'[\s_\-()（）【】\[\]#:：,，、/\\.。*]')
NUM = re.compile(r'(\d+)#|([A-H])轴|([ABC])面|([一二三四])层', re.I)


def clean(s, var=False):
    if var:
        s = re.sub(r'^(ID|OD|AD|DA)_', '', s)
        s = re.sub(r'_?For[A-Z]+$|\[\d+\]$', '', s, flags=re.I)
    s = s.upper()
    for a, b in SYN:
        s = s.replace(a.upper(), b.upper())
    s = re.sub(r'新增$', '', s)
    return STRIP.sub('', s)


def tags(s):
    """提取编号类标识（1#、A轴、二层、机头A…）用来拦截错配"""
    s2 = s
    for a, b in SYN:
        if a in ('A面涂布', 'B面涂布', 'A面牵引', 'B面牵引'):
            s2 = s2.replace(a, b)
    out = set()
    for m in NUM.finditer(s2):
        if m.group(1):
            out.add(f'#{int(m.group(1))}')
        elif m.group(2):
            out.add(m.group(2).upper() + '轴')
        elif m.group(4):
            out.add(m.group(4) + '层')
    for m in re.finditer(r'(机头|出料)([AB])', s2):
        out.add(m.group(1) + m.group(2) + '站')
    # 注：数组下标 [n] 不一定等于设备编号（如 [27]=二层07#），不参与编号比较
    return out


def similarity(func, var):
    a, b = clean(func), clean(var, True)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    # 字符覆盖率：较短一方的字符有多少出现在另一方（应对词序调换）
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    cover = sum(1 for ch in short if ch in long_) / len(short)
    return max(seq, cover * 0.9)


# ---------- 比对 ----------
def fb_key(wire, func, var):
    return f'{wire}|{func}|{var}'


def compare(drawing, program, feedback=None):
    feedback = feedback or {}
    array_mods = set()
    for p in program:
        if p.get('array'):
            k, mod, _ = p['wire'].split('-')
            array_mods.add(('ID' if k == 'I' else 'OD', mod))
    for d in drawing:
        # I/O 映射里一律按位命名 ID_H98_00（程序里的 ID_H98[0] 数组是另一段程序转换的），所以不用数组写法
        d['prog_wire'] = prog_name(d['wire'])
    valid_wires = {d['wire'] for d in drawing if d['void'] != '整页作废'}
    fkey = lambda f: re.sub(r'\W', '', f or '')
    # 同名功能：作废页 ↔ 有效页，用来识别“点位挪动”
    func_valid, func_void = {}, {}
    for d in drawing:
        if not d['function']:
            continue
        tgt = func_void if d['void'] == '整页作废' else func_valid
        tgt.setdefault(fkey(d['function']), []).append(d['wire'])
    drawing = [d for d in drawing if not (d['void'] == '整页作废' and d['wire'] in valid_wires)]
    by_wire = {}
    for p in program:
        by_wire.setdefault(p['wire'], []).append(p)
    draw_count = {}
    for d in drawing:
        if d['wire'] and d['void'] != '整页作废':
            draw_count.setdefault(d['wire'], []).append(d['page'])
    seen = set()
    results = []
    for d in drawing:
        w = d['wire']
        progs = by_wire.get(w, []) if w else []
        seen.add(w)
        act = [p for p in progs if p['active']]
        cmt = [p for p in progs if not p['active']]
        page_void = d['void'] == '整页作废'
        void = d['void'] in ('作废', '整页作废')
        func = d['function']
        rec = {**d, 'var': '', 'section': '', 'line': '', 'raw': '', 'score': '', 'status': '',
               'note': d.get('note', ''), 'drawing_note': d.get('note', ''), 'judge': '', 'remark': ''}
        if not w:
            rec.update(status='⚠️ 图纸线号缺失', note='PDF中该点未读到线号，请看图确认')
        elif len(act) > 1:
            rec.update(status='❌ 重复赋值',
                       var=' / '.join(p['var'] for p in act),
                       note='同一线号在程序中被多次使用')
        elif act:
            p = act[0]
            sc = similarity(func, p['var'])
            rec.update(var=p['var'], section=p['section'], line=p['line'], raw=p['raw'],
                       score=round(sc, 2))
            tf, tv = tags(func), tags(p['var'])
            conflict = []
            for kind in ('#', '轴', '层', '站'):
                f_ = {t for t in tf if kind in t}
                v_ = {t for t in tv if kind in t}
                if f_ and v_ and not (f_ & v_):
                    conflict.append(f'图纸{sorted(f_)}≠程序{sorted(v_)}')
            if page_void:
                moved = [x for x in func_valid.get(fkey(func), []) if x != w]
                if moved:
                    rec.update(status='🔀 点位挪动：程序需改线号',
                               note=f'新图纸中同名点在 {"、".join(moved)}，程序这行线号应改过去')
                else:
                    rec.update(status='❌ 图纸整页作废但程序在用', note='该页整页打叉（旧版页面），新图纸中未找到同名点')
            elif void:
                rec.update(status='❌ 图纸作废但程序在用')
            elif not func:
                rec.update(status='⚠️ 图纸为空/备用但程序在用')
            elif conflict:
                rec.update(status='❌ 编号不一致', note='；'.join(conflict))
            elif sc >= 0.75:
                rec.update(status='✅ 一致')
            elif sc >= 0.50:
                rec.update(status='⚠️ 名称相似，请确认')
            else:
                rec.update(status='❌ 名称不一致')
        elif cmt:
            p = cmt[0]
            rec.update(var=p['var'], section=p['section'], line=p['line'], raw=p['raw'])
            if page_void:
                rec.update(status='✅ 整页作废，程序已注释')
            elif void or not func or '备用' in func:
                rec.update(status='✅ 停用（图纸作废/备用）')
            else:
                rec.update(status='⚠️ 程序已注释，图纸仍在用', note=p['comment'])
        else:
            if page_void:
                rec.update(status='✅ 整页作废，程序未用')
            elif void or not func or '备用' in func:
                rec.update(status='✅ 备用/作废，程序未用')
            else:
                old = [x for x in func_void.get(fkey(func), []) if x in by_wire and any(p['active'] for p in by_wire[x])]
                if old:
                    ov = next(p['var'] for p in by_wire[old[0]] if p['active'])
                    rec.update(status='🔀 点位挪动：程序需改线号', var=ov,
                               note=f'从作废页的 {old[0]} 挪来，程序变量 {ov} 仍接在旧线号上')
                else:
                    rec.update(status='❌ 程序缺少该点', note='新增点？需建变量和映射')

        # 图纸上同一线号出现在多页 → 图纸本身有错
        pages = draw_count.get(w, [])
        if len(pages) > 1 and not rec['status'].startswith('✅ 备用'):
            rec['status'] = '❌ 图纸线号重复'
            rec['note'] = (rec['note'] + '；' if rec['note'] else '') + f'同一线号出现在第 {"、".join(pages)} 页'

        results.append(rec)

    extra = [p for p in program if p['wire'] not in seen]
    rematch_by_name(results)
    auto_enable(results, program)
    pair_moved_arrays(results, extra)
    apply_feedback(results, feedback, program)
    return results, extra


# ---------- 人工反馈解读 ----------
NAME_RULES = [('机头1#', '机头A'), ('机头2#', '机头B'), ('A面涂布', '机头A'), ('B面涂布', '机头B'),
              ('A面牵引', '出料A'), ('B面牵引', '出料B'), ('过滤器出口压力', '滤后压力'),
              ('过滤器压力', '滤后压力'), ('AD值', ''), ('-新增', ''), ('新增', '')]
STATION_WORDS = ['机头A', '机头B', '出料A', '出料B', '放卷', '收卷隔断', '收卷', '一层烘箱', '二层烘箱', '烘箱']
AREA_STATION = {}   # 线号区字母 → 工位（从程序变量统计）


def learn_area_station(program):
    from collections import Counter, defaultdict
    vote = defaultdict(Counter)
    for p in program:
        for st in STATION_WORDS:
            if st in p['var']:
                vote[p['wire'].split('-')[1][0]][st] += 1
                break
    AREA_STATION.update({a: c.most_common(1)[0][0] for a, c in vote.items()})


def suggest_name(wire, func):
    """按“前缀_工位_功能”生成新变量名"""
    k, mod, _ = wire.split('-')
    pre = {'I': 'ID', 'O': 'OD'}.get(k, k)
    f = func or ''
    for a, b in NAME_RULES:
        f = f.replace(a, b)
    f = re.sub(r'[（(][^）)]*[）)]|\*|\s', '', f)
    st = next((x for x in STATION_WORDS if x in f), '') or AREA_STATION.get(mod[0], '')
    f = f.replace(st, '', 1).strip('-_')
    return f'{pre}_{st}_{f}' if st else f'{pre}_{f}'


VAR_RE = re.compile(r'(?:(?:ID|OD|AD|DA)_)?[A-Za-z一-鿿][\w一-鿿]*_[\w一-鿿]+')


def interpret(judge, remark):
    j = (judge or '').strip()
    t = f'{j} {remark or ""}'
    if j == '对':
        return 'confirm', ''
    if j == '错' and VAR_RE.search(remark or ''):
        return 'assign', VAR_RE.search(remark).group(0)
    if re.search(r'注释', t) and not VAR_RE.search(t.replace('注释', '')):
        return 'drop', ''
    if '以图纸为准' in t:
        return 'follow_new', ''
    if '程序正确的变量' in t:
        return 'new', ''
    if re.search(r'[A-H]是(机头|出料)[AB]', t):
        return 'area_fix', t
    if j == '错' and not VAR_RE.search(remark or ''):
        return 'wrong', ''
    if '图纸搞错' in t or '图纸错' in t:
        return 'drawing_error', ''
    if '新增' in j:
        return 'new', ''
    if '同机头A' in t:
        return 'same_as_A', ''
    if '按新点位' in t:
        return 'follow_new', ''
    if re.search(r'删掉|无了|忽略|不用', t):
        return 'drop', ''
    if '保留' in t:
        return 'keep', ''
    m = VAR_RE.search((remark or '') if j == '错' else t.replace('有，程序变量是', ' ').replace('就是这个', ' '))
    if m:
        return 'assign', m.group(0)
    if '新增' in t or '滤后压力' in t or '机头1' in t:
        return 'new', ''
    return 'note', ''


def apply_feedback(results, feedback, program):
    learn_area_station(program)
    var_wires = {}
    for p in program:
        var_wires.setdefault(p['var'], []).append(p)
    by_wire = {r['wire']: r for r in results if r['wire']}
    later = []
    for r in results:
        fb = feedback.get(fb_key(r['wire'], r['function'], r['var']))
        if not fb:
            continue
        r['judge'], r['remark'] = fb.get('judge', ''), fb.get('remark', '')
        act, var = interpret(r['judge'], r['remark'])
        if act == 'drawing_error' and '更正' not in (r.get('note') or ''):
            # 本页线号没写错（错的是另一页、已在提取时更正），这条判断不适用
            r['judge'], r['remark'] = '', ''
            continue
        r['action'] = act
        old = r['var']
        if act == 'confirm':
            r['status'] = '🔀 已确认挪动：程序改线号' if r['status'].startswith('🔀') else '✅ 已确认'
            r['new_var'] = old
        elif act == 'wrong':
            r['status'] = '❓ 你判定为错，待定'
        elif act == 'drawing_error':
            r['status'] = '🛠 图纸线号有误（已在提取时更正）'
        elif act == 'drop':
            r['status'] = '✅ 不用'
        elif act == 'keep':
            r['status'] = '✅ 保留程序行'
            r['new_var'] = old
        elif act == 'assign':
            if not var.startswith(('ID_', 'OD_', 'AD_', 'DA_')):
                var = {'I': 'ID', 'O': 'OD'}.get(r['wire'][0], r['wire'][:2]) + '_' + var
            r['new_var'] = var
            cur = [p for p in var_wires.get(var, []) if p['wire'] != r['wire']]
            if cur:
                r['status'] = '🔀 指定变量：程序改线号'
                r['note'] = f'{var} 原来接在 ' + '、'.join(
                    prog_name(p['wire']) + ('' if p['active'] else '(已注释)') for p in cur)
            elif var in var_wires:
                r['status'] = '✅ 指定变量'
            else:
                r['status'] = '🆕 指定变量（程序里没有，需新建）'
        elif act == 'new':
            r['new_var'] = suggest_name(r['wire'], r['function'])
            r['status'] = '🆕 需新增变量'
            r['note'] = '建议名称，可改'
        elif act == 'same_as_A':
            later.append(r)
        elif act == 'area_fix':
            # 备注形如“C是出料A G是出料B”：变量工位与线号区不符的，挪到对应区同模块同通道
            amap = {a: st for a, st in re.findall(r'([A-H])是((?:机头|出料)[AB])', var)}
            r['status'] = '✅ 按线号区规则处理'
            for pv in [p for p in program if p['wire'] == r['wire'] and p['active']]:
                m_ = re.search(r'(机头|出料)[AB]', pv['var'])
                k_, mod_, ch_ = r['wire'].split('-')
                if not m_ or amap.get(mod_[0]) == m_.group(0):
                    r['new_var'] = pv['var']
                    continue
                area = next((a for a, st in amap.items() if st == m_.group(0)), None)
                tgt = by_wire.get(f'{k_}-{area}{mod_[1:]}-{ch_}') if area else None
                if tgt is not None:
                    tgt['displaced'] = tgt.get('var')
                    tgt['new_var'], tgt['action'] = pv['var'], 'assign'
                    tgt['status'] = '🔀 按线号区规则改线号'
                    tgt['note'] = f'{pv["var"]} 原接 {prog_name(r["wire"])}，按“{var.strip()}”改到这里'
        elif act == 'follow_new':
            if r['status'].startswith('🔀'):
                r['status'] = '🔀 按新点位：程序改线号'
            elif old:
                r['status'] = '🛠 按新点位：注释掉程序这一行'
            else:
                r['status'] = '✅ 按新点位，无需处理'
        else:
            r['status'] = '❓ 有备注，待处理'
    # 新增点：同一模块里有“同模板”的兄弟点（功能只差编号/层）已有变量 → 套用其命名
    for r in results:
        if r.get('action') == 'new':
            nm = sibling_name(r, results)
            if nm:
                r['new_var'] = nm
                r['note'] = '按同类点的命名套用，可改'
    # 同机头A：找对应 A 侧同模块同通道的点（F→B），沿用其变量并把 机头A 换成 机头B
    swap = {'F': 'B', 'G': 'C'}
    for r in later:
        k, mod, ch = r['wire'].split('-')
        a = by_wire.get(f'{k}-{swap.get(mod[0], mod[0])}{mod[1:]}-{ch}')
        base = ((a or {}).get('new_var') or (a or {}).get('var') or '') if a else ''
        if base:
            r['new_var'] = base.replace('机头A', '机头B').replace('出料A', '出料B')
            r['status'] = '🆕 需新增变量（同机头A）'
            r['note'] = f'参照 {prog_name(a["wire"])} {base}'
        else:
            r['new_var'] = suggest_name(r['wire'], r['function'])
            r['status'] = '🆕 需新增变量（同机头A，未找到参照，按图纸命名）'
    dedupe_new_names(results)


TEMPLATE_RE = re.compile(r'(\d+)#|([一二])层')


def _template(func):
    return TEMPLATE_RE.sub('§', re.sub(r'\W', '', func or ''))


def _nums(func):
    return re.findall(r'(\d+)#', func or ''), re.findall(r'([一二])层', func or '')


def sibling_name(r, results):
    """功能文字去掉编号/层后相同、且已有变量的点，按编号替换出新变量名"""
    tpl = _template(r['function'])
    if '§' not in tpl:
        return ''
    for o in results:
        v = o.get('new_var') or o.get('var')
        if o is r or not v or '/' in v or _template(o['function']) != tpl:
            continue
        (on, ol), (rn, rl) = _nums(o['function']), _nums(r['function'])
        if len(on) != len(rn) or len(ol) != len(rl):
            continue
        nm = v
        for x, y in zip(ol, rl):
            nm = nm.replace(x + '层', y + '层')
        for x, y in zip(on, rn):
            if x in nm:
                nm = nm.replace(x, y.zfill(len(x)), 1)
            elif x.lstrip('0') and x.lstrip('0') in nm:
                nm = nm.replace(x.lstrip('0'), y.lstrip('0'), 1)
            else:
                return ''
        if nm != v:
            return nm
    return ''


def clean_var_name(name):
    """变量名只保留 字母/数字/下划线/汉字；“二01”补成“二层01”；数组元素名不动"""
    if not name or '[' in name:
        return name
    name = re.sub(r'([一二三四])(?=\d)', r'\1层', name)
    name = re.sub(r'[^\w]', '', name)
    name = re.sub(r'_+', '_', name).strip('_')
    return name


def dedupe_new_names(results):
    """工具生成的名字：去特殊符号、去重复词；重名时同模块加 _通道号，跨模块加 _模块号通道号"""
    from collections import defaultdict
    gen = lambda r: r.get('action') in ('new', 'same_as_A')
    for r in results:
        if r.get('new_var') and gen(r):
            v = r['new_var']
            if r.get('action') == 'new':   # 只处理工具生成的名字，你指定的名字不动
                v = re.sub(r'([一-鿿]{2,4})\1', r'\1', v)
            r['new_var'] = clean_var_name(v)
    groups = defaultdict(list)
    for r in results:
        if r.get('new_var') and gen(r):
            groups[r['new_var']].append(r)
    by_wire = {r['wire']: r for r in results if r['wire']}

    def neighbor_ceng(x):
        k, mod, ch = x['wire'].split('-')
        w = 2 if k in 'IO' else 1
        cs = []
        for dc in (-1, 1):
            n = by_wire.get(f'{k}-{mod}-{int(ch) + dc:0{w}d}')
            c = re.findall(r'([一二])层?(?=\d)', (n or {}).get('function') or '')
            if c:
                cs.append(c[0])
        return cs

    for v, rs in groups.items():
        if len(rs) < 2:
            continue
        for x in rs:
            mine = re.findall(r'([一二])层', x['new_var'])
            nb = neighbor_ceng(x)
            if mine and len(nb) == 2 and nb[0] == nb[1] != mine[0]:
                x['new_var'] = x['new_var'].replace(mine[0] + '层', nb[0] + '层', 1)
                x['note'] = (f'图纸疑点：本点写“{mine[0]}层”，但同模块前后都是“{nb[0]}层”，'
                             f'按{nb[0]}层命名，请核对图纸')
        rs = [x for x in rs if x['new_var'] == v]
        if len(rs) < 2:
            continue
        mods = {x['wire'].split('-')[1] for x in rs}
        for x in rs:
            _, mod, ch = x['wire'].split('-')
            x['new_var'] = f'{v}_{int(ch)}' if len(mods) == 1 else f'{v}_{mod}{int(ch):02d}'





def _name_keys(s):
    """变量名/功能里的 编号集合 和 层"""
    s = s or ''
    nums = frozenset(int(x) for x in re.findall(r'(?<![A-Za-z])(\d{1,2})(?![A-Za-z\d])', s.replace('_', ' ')))
    ceng = frozenset(re.findall(r'([一二三四])层', s))
    return nums, ceng


def auto_enable(results, program):
    """旧程序把这一行注释了，但新图纸有这个点，且变量名和图纸功能明确对得上（相似≥0.8、编号/层/工位一致、
    变量没在别处使用）→ 自动启用"""
    active = {p['var'] for p in program if p['active']}
    for r in results:
        if not r['status'].startswith('⚠️ 程序已注释') or not r['var'] or r['var'] in active:
            continue
        if r['void']:
            continue            # 图纸上该点作废，保持注释
        vn, vc = _name_keys(r['var'])
        fn, fc = _name_keys(r['function'])
        tv, tf = tags(r['var']), tags(r['function'])
        if (vn and fn and vn != fn) or (vc and fc and vc != fc):
            continue
        if any(t for t in tv if '站' in t) and not (tv & tf):
            continue
        if similarity(r['function'], r['var']) >= 0.8:
            r['status'] = '✅ 启用（原程序注释，图纸有此点）'
            r['action'] = 'enable'
            r['new_var'] = r['var']
    # 同组一致：同模块相邻（±3 通道）已启用的点，变量名只差动作词的，一起启用
    ACT = r'(下降|抬起|伸出|缩回|前进|后退|上升|打开|关闭|夹紧|松开|到位)位?$'
    stem = lambda v: re.sub(ACT, '', v)
    changed = True
    while changed:
        changed = False
        en = [r for r in results if r.get('action') == 'enable']
        for r in results:
            if not r['status'].startswith('⚠️ 程序已注释') or not r['var'] or r['var'] in active or r['void']:
                continue
            if stem(r['var']) == r['var'] or similarity(r['function'], r['var']) < 0.6:
                continue
            k, mod, ch = r['wire'].split('-')
            for e in en:
                ek, emod, ech = e['wire'].split('-')
                if (ek, emod) == (k, mod) and abs(int(ech) - int(ch)) <= 3 and stem(e['var']) == stem(r['var']):
                    r['status'] = '✅ 启用（同组其他点已启用，叫法不同）'
                    r['action'] = 'enable'
                    r['new_var'] = r['var']
                    changed = True
                    break


def rematch_by_name(results):
    """页面作废后挪到新页的点：按“变量名”找新图纸上编号、层都一致的点，而不是看旧线号上写的功能。
    例：旧程序 E02[12] 因点位损坏接的是 二层01#，变量名 ID_二层烘箱_01… 应接到新图纸“二层01#”。"""
    moved = [r for r in results if r['status'].startswith('🔀 点位挪动') and r['var']
             and '从作废页' in (r.get('note') or '')]
    pool = moved + [r for r in results if r['status'] == '❌ 程序缺少该点' and r['function']]
    taken = {}
    pairs = []
    for r in moved:
        vn, vc = _name_keys(r['var'])
        for c in pool:
            if c['wire'].split('-')[0] != r['wire'].split('-')[0]:
                continue
            fn, fc = _name_keys(c['function'])
            if vn and vn == fn and vc == fc:
                pairs.append((similarity(c['function'], r['var']), r, c))
    pairs.sort(key=lambda x: -x[0])
    assigned, used = {}, set()
    for sc, r, c in pairs:
        if id(r) in assigned or id(c) in used or sc < 0.5:
            continue
        assigned[id(r)] = c
        used.add(id(c))
    snap = {id(r): (r['var'], r['note']) for r in moved}
    for r in moved:
        c = assigned.get(id(r))
        if c is None or c is r:
            continue
        var, note = snap[id(r)]
        if id(r) not in used:          # 原落点没有别的变量来接 → 恢复为缺少
            r.update(var='', status='❌ 程序缺少该点', note='新增点？需建变量和映射')
        c.update(var=var, status='🔀 点位挪动：程序需改线号',
                 note=note.split('，')[0] + '（按变量名匹配编号/层）')


def _wire_parts(w):
    # 'AD-E59-5' -> ('AD', 'E', '59', 5)；'I-E5C-03' -> ('I', 'E', '5C', 3)
    k, mod, ch = w.split('-')
    return k, mod[0], mod[1:], int(ch)


def pair_moved_arrays(results, extra):
    """程序里有、图纸上没有的线号 ↔ 图纸上有、程序里缺的线号：
    同类信号、同一区、程序变量名去掉下标后相同的一组，若两边数量相等且名称相似，按顺序配对。
    典型场景：旧程序 8 通道模块 AD_E59_1~8 → 新图纸拆成两个 4 通道 E59 + E5A。"""
    groups = {}
    for p in extra:
        if not p['active']:
            continue
        k, area, mod, ch = _wire_parts(p['wire'])
        base = re.sub(r'\[\d+\]$', '', p['var'])
        groups.setdefault((k, area, base), []).append(p)
    missing = [r for r in results if r['status'] == '❌ 程序缺少该点']
    for (k, area, base), progs in groups.items():
        cands = [r for r in missing if r['status'] == '❌ 程序缺少该点'
                 and _wire_parts(r['wire'])[:2] == (k, area)
                 and similarity(r['function'], base) >= 0.6]
        if not cands or len(cands) != len(progs):
            continue
        progs.sort(key=lambda p: (len(_wire_parts(p['wire'])[2]), _wire_parts(p['wire'])[2], _wire_parts(p['wire'])[3]))
        cands.sort(key=lambda r: (_wire_parts(r['wire'])[2], _wire_parts(r['wire'])[3]))
        for p, r in zip(progs, cands):
            r.update(status='🔀 疑似挪动：按顺序配对，请确认', var=p['var'], section=p['section'],
                     line=p['line'], raw=p['raw'],
                     note=f'程序旧线号 {p["wire"]} 在新图纸上不存在；同组 {len(progs)} 个点按顺序配到这里')
            p['suggest'] = r['wire']


# ---------- 输出 ----------
FILL = {
    '✅': PatternFill('solid', fgColor='C6EFCE'),
    '⚠️': PatternFill('solid', fgColor='FFEB9C'),
    '❌': PatternFill('solid', fgColor='FFC7CE'),
    '🔀': PatternFill('solid', fgColor='BDD7EE'),
    '🆕': PatternFill('solid', fgColor='E2EFDA'),
    '🛠': PatternFill('solid', fgColor='FCE4D6'),
    '❓': PatternFill('solid', fgColor='FF9999'),
}


def write_io_paste(wb, results):
    """按模块、通道顺序列出程序线号，复制「变量」列粘贴到 Sysmac I/O 映射"""
    ws = wb.create_sheet('IO映射粘贴', 1)
    ws.append(['页码', '模块型号', '模块', '通道', '变量（复制这列）', '变量注释（图纸功能）', '说明'])
    for c in ws[1]:
        c.font = Font(bold=True)
    mods = {}
    for r in results:
        if not r['wire'] or r['void'] == '整页作废':
            continue
        k, mod, ch = r['wire'].split('-')
        mods.setdefault((k, mod), []).append((int(ch), r))
    order = sorted(mods, key=lambda km: min(int(r['page']) for _, r in mods[km]))
    for km in order:
        rows = sorted(mods[km], key=lambda x: x[0])
        seen = set()
        for ch, r in rows:
            if ch in seen:
                continue
            seen.add(ch)
            note = '图纸作废' if r['void'] else ('图纸无I/O表，模块和通道请核对' if '仅在现场接线页' in (r.get('note') or '') else '')
            ws.append([int(r['page']), r.get('module', ''), prog_name(f'{km[0]}-{km[1]}-0').rsplit('_', 1)[0], ch,
                       r.get('prog_wire', ''), r['function'] or '', note])
        ws.append([None])
    for i, w in enumerate([6, 14, 9, 6, 18, 40, 10], 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
    ws.freeze_panes = 'A2'


def write_xlsx(results, extra, path, drawing_doubts=None):
    wb = Workbook()
    ws = wb.active
    ws.title = '对点结果'
    hdr = ['结果', '你的判断', '备注', '线号', '图纸原线号', '图纸功能', '元件', '图纸作废', '程序变量', '新程序变量', '相似度',
           '程序段', '行', '程序原文', '说明', '页码', '模块型号', '图纸标题', '槽位']
    ws.append(hdr)
    order = {'❓': 0, '❌': 1, '🆕': 2, '🔀': 3, '🛠': 4, '⚠️': 5, '✅': 6}

    def okey(r):
        return order['⚠️' if r['status'].startswith('⚠') else r['status'][:1]]

    fill_in = PatternFill('solid', fgColor='DDEBF7')
    for r in sorted(results, key=lambda r: (okey(r), r['wire'] or 'ZZZ')):
        ws.append([r['status'], r['judge'], r['remark'], r.get('prog_wire', '') or prog_name(r['wire']), r['wire'], r['function'], r['device'],
                   r['void'], r['var'], r.get('new_var', ''), r['score'], r['section'], r['line'], r['raw'], r['note'],
                   int(r['page']), r.get('module', ''), r['title'], r['slot']])
        key = '⚠️' if r['status'].startswith('⚠') else r['status'][:1]
        ws.cell(ws.max_row, 1).fill = FILL[key]
        ws.cell(ws.max_row, 2).fill = fill_in
        ws.cell(ws.max_row, 3).fill = fill_in
    dv = DataValidation(type='list', formula1='"对,错,忽略"', allow_blank=True,
                        prompt='对=名称其实一致；错=确实有问题；忽略=不用管', promptTitle='你的判断')
    ws.add_data_validation(dv)
    dv.add(f'B2:B{ws.max_row}')
    widths = [24, 9, 24, 11, 13, 34, 10, 9, 34, 30, 7, 22, 5, 44, 30, 6, 14, 26, 12]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = w
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.freeze_panes = 'E2'
    ws.auto_filter.ref = ws.dimensions

    ws2 = wb.create_sheet('程序有图纸无')
    ws2.append(['线号', '程序变量', '是否注释', '建议新线号', '程序段', '行', '程序原文'])
    for p in sorted(extra, key=lambda p: p['wire']):
        ws2.append([prog_name(p['wire']), p['var'], '' if p['active'] else '已注释', prog_name(p.get('suggest', '')),
                    p['section'], p['line'], p['raw']])
        if p['active']:
            ws2.cell(ws2.max_row, 1).fill = FILL['❌']
    for i, w in enumerate([11, 34, 8, 12, 24, 5, 50], 1):
        ws2.column_dimensions[ws2.cell(1, i).column_letter].width = w

    write_io_paste(wb, results)

    ws4 = wb.create_sheet('同义词')
    ws4.append(['图纸叫法', '程序叫法', '说明：比较名称前，左列词会替换成右列词。右列留空=忽略该词。可直接在下面加行。'])
    for a, b in SYN:
        ws4.append([a, b])
    ws4.column_dimensions['A'].width = 18
    ws4.column_dimensions['B'].width = 18
    ws4.column_dimensions['C'].width = 70
    for c in ws4[1]:
        c.font = Font(bold=True)

    if drawing_doubts:
        ws5 = wb.create_sheet('图纸疑点', 1)
        ws5.append(['编号', '类别', '线号', '页码', '图纸功能', '说明', '你的结论'])
        n = 0
        for cat, rows, why in drawing_doubts:
            n += 1
            for i, d in enumerate(rows):
                ws5.append([n if i == 0 else '', cat if i == 0 else '', prog_name(d['wire']), int(d['page']),
                            d['function'], why if i == 0 else '', ''])
                ws5.cell(ws5.max_row, 7).fill = PatternFill('solid', fgColor='DDEBF7')
                if cat.startswith(('模块号', 'A/B对称：写成')):
                    ws5.cell(ws5.max_row, 2).fill = FILL['❌']
        for i, wd in enumerate([5, 22, 12, 6, 36, 60, 24], 1):
            ws5.column_dimensions[ws5.cell(1, i).column_letter].width = wd
        for c in ws5[1]:
            c.font = Font(bold=True)
        ws5.freeze_panes = 'A2'

    ws3 = wb.create_sheet('统计')
    from collections import Counter
    for k, v in Counter(r['status'] for r in results).most_common():
        ws3.append([k, v])
    ws3.append(['程序有、图纸无（启用中）', sum(1 for p in extra if p['active'])])
    ws3.column_dimensions['A'].width = 30
    try:
        wb.save(path)
    except PermissionError:
        import time
        alt = path.replace('.xlsx', time.strftime('_%H%M%S.xlsx'))
        wb.save(alt)
        print(f'⚠ {os.path.basename(path)} 正被打开，另存为 {os.path.basename(alt)}')


def main():
    csv_path, folder, out = sys.argv[1:4]
    fb_path = os.path.join(os.path.dirname(os.path.abspath(out)), '对点反馈.json')
    feedback = json.load(open(fb_path, encoding='utf-8')) if os.path.exists(fb_path) else {}
    drawing = read_points_csv(csv_path)
    if not drawing or not any(d.get('wire') for d in drawing):
        sys.exit('图纸点表没有有效线号，已停止对点；请检查图纸和提取格式。')
    try:
        check_unsupported_program(folder)
    except ValueError as e:
        sys.exit(str(e))
    program = parse_program(folder)
    if not program:
        sys.exit('旧程序没有识别出 I/O 映射行，已停止对点；请检查程序是否为空或线号格式尚不支持。')
    results, extra = compare(drawing, program, feedback)
    from check_drawing import find_doubts
    doubts = find_doubts(drawing)
    write_xlsx(results, extra, out, doubts)
    print(f'图纸疑点 {len(doubts)} 处（见报告「图纸疑点」表）')
    from collections import Counter
    print(f'图纸点 {len(drawing)}，程序映射行 {len(program)}，已有人工反馈 {len(feedback)} 条')
    for k, v in Counter(r['status'] for r in results).most_common():
        print(f'  {k}: {v}')
    print(f'  程序有图纸无（启用中）: {sum(1 for p in extra if p["active"])}')
    print('报告:', os.path.abspath(out))


if __name__ == '__main__':
    main()
