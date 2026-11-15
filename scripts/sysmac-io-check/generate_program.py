"""按对点结果生成新的 Input/Output 映射程序段。

用法: python generate_program.py <图纸点表.csv> <旧程序段目录> <输出目录> [反馈json]

以旧程序段为底稿逐行改写（保留 NOT、换算公式、注释、空行）：
  - 变量在新图纸上的线号变了 → 只替换线号，行尾加 // 原 xxx
  - 新图纸上没有/作废/已被别的变量占用 → 整行注释掉，写明原因
  - 新增点 → 追加到同模块所在的程序段末尾
输出:
  <段名>.st.txt     新程序段全文，整段粘贴回 Sysmac
  新增变量.txt       需要在全局变量表新建的变量（名称<TAB>类型<TAB>注释），可直接粘贴
  变更清单.xlsx      每一处改动 + 需要人工确认的行
"""
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import compare_io as C  # noqa: E402
from check_drawing import norm_func  # noqa: E402
from extract_drawing_io import read_points_csv  # noqa: E402
from openpyxl import Workbook  # noqa: E402
from openpyxl.styles import Font, PatternFill  # noqa: E402

KEEP_PREFIX = ('✅ 启用', '✅ 一致', '✅ 已确认', '✅ 指定变量', '✅ 保留', '⚠️ 名称相似', '⚠️ 图纸为空', '🔀', '🆕')
DROP_PREFIX = ('🛠', '✅ 不用', '✅ 备用', '✅ 停用', '✅ 整页', '✅ 按新点位')


def token(wire, array_mods):
    """线号 → 程序里的写法"""
    k, mod, ch = wire.split('-')
    if k in ('AD', 'DA'):
        return f'{k}_{mod}_{int(ch)}'
    pre = 'ID' if k == 'I' else 'OD'
    return f'{pre}_{mod}_{int(ch):02d}'     # 一律按位写法，与 I/O 映射一致（不用 ID_E09[0] 数组）


def final_var(r):
    st = r['status']
    if r.get('action') == 'keep' and r['var']:
        return r['var']   # 你判断“保留”的，即使图纸作废也保留
    if r['void'] or st.startswith(DROP_PREFIX):
        return None
    if r.get('new_var'):
        return r['new_var']
    if st.startswith(KEEP_PREFIX) and r['var'] and '/' not in r['var']:
        return r['var']
    return None


def old_token(raw, wire):
    """在原文里找出这条线号的写法（ID_E01[0] / ID_A01_00 / AD_C05_01）"""
    k, mod, ch = wire.split('-')
    pre = {'I': 'ID', 'O': 'OD'}.get(k, k)
    for m in re.finditer(rf'\b{pre}_{mod}(?:_(\d+)|\[(\d+)\])', raw):
        if int(m.group(1) or m.group(2)) == int(ch):
            return m.group(0)
    return None


PLACEHOLDER_VAR = '空块占位'
KW_ONLY = re.compile(r'^(IF\b.*|THEN|ELSE|ELSIF\b.*|END_IF;?|.*\bTHEN)$', re.I)


def _active(lines):
    """逐行返回去掉注释后的有效代码（处理 // 和跨行 (* *)）"""
    res, in_blk = [], False
    for l in lines:
        code, i = '', 0
        while i < len(l):
            if in_blk:
                j = l.find('*)', i)
                if j < 0:
                    i = len(l)
                else:
                    in_blk, i = False, j + 2
            elif l.startswith('(*', i):
                in_blk, i = True, i + 2
            elif l.startswith('//', i):
                break
            else:
                code += l[i]
                i += 1
        res.append(code.strip())
    return res


def fix_empty_ifs(lines):
    """IF 结构里所有分支都没有有效语句时，把 IF/ELSIF/ELSE/END_IF 行一起注释掉（Sysmac 不接受空 IF）"""
    n = 0
    while True:
        act = _active(lines)
        stack, target = [], None
        for i, c in enumerate(act):
            if not c:
                continue
            u = c.upper()
            if re.match(r'IF\b', u):
                stack.append({'kw': [i], 'stmt': False, 'open': 'THEN' not in u,
                              'br': [[i, False]]})   # 分支：[起始行, 是否有语句]
            elif stack and stack[-1]['open']:
                stack[-1]['kw'].append(i)
                if 'THEN' in u:
                    stack[-1]['open'] = False
            elif re.match(r'(ELSIF\b|ELSE\b)', u) and stack:
                stack[-1]['kw'].append(i)
                stack[-1]['br'].append([i, False])
                stack[-1]['open'] = u.startswith('ELSIF') and 'THEN' not in u
            elif re.match(r'END_IF', u) and stack:
                f = stack.pop()
                f['kw'].append(i)
                if not f['stmt']:
                    target = f
                    break
                if stack:
                    stack[-1]['stmt'] = True
                    stack[-1]['br'][-1][1] = True
                else:
                    # 有语句的 IF：处理空分支
                    for bi, (start, has) in enumerate(f['br']):
                        if has:
                            continue
                        if act[start].upper().startswith('ELSE') and not act[start].upper().startswith('ELSIF'):
                            lines[start] = '//' + lines[start].rstrip() + '   // 新图纸注释：ELSE 分支语句已全部注释'
                        else:
                            end = f['br'][bi + 1][0] if bi + 1 < len(f['br']) else i
                            lines.insert(end, f'{PLACEHOLDER_VAR}:=FALSE;   // 本分支语句已全部注释，占位')
                        target = 'restart'
                    if target:
                        break
            elif stack:
                stack[-1]['stmt'] = True
                stack[-1]['br'][-1][1] = True
        if not target:
            return lines, n
        if target == 'restart':
            n += 1
            continue
        for i in target['kw']:
            lines[i] = '//' + lines[i].rstrip() + '   // 新图纸注释：IF 内语句已全部注释'
        n += 1


def postprocess_section(lines, stats):
    out, cur = [], []

    def flush():
        if not cur:
            return
        body, nfix = fix_empty_ifs(cur[1:] if cur[0].startswith('//====== ST块') else cur[:])
        head = cur[:1] if cur[0].startswith('//====== ST块') else []
        stats['空IF注释'] += nfix
        if head and not any(_active(body)):
            body = [f'{PLACEHOLDER_VAR}:=FALSE;   // 本块点位已全部注释，占位语句防止编译报错（局部变量 BOOL）'] + body
            stats['空块占位'] += 1
        out.extend(head + body)

    for l in lines:
        if l.startswith('//====== ST块'):
            flush()
            cur = [l]
        else:
            cur.append(l)
    flush()
    return out


def _mod_re(wire):
    k, mod, _ = wire.split('-')
    if k in ('AD', 'DA'):
        return re.compile(rf'\b{k}_{mod}_(\d+)\b')
    pre = 'ID' if k == 'I' else 'OD'
    return re.compile(rf'\b{pre}_{mod}(?:_(\d+)\b|\[(\d+)\])')


def _depths(lines):
    """每行开始前的 IF 嵌套深度（按有效代码算）"""
    d, out = 0, []
    for c in _active(lines):
        out.append(d)
        u = c.upper()
        if re.match(r'IF\b', u):
            d += 1
        elif u.startswith('END_IF'):
            d = max(0, d - 1)
    return out


def place_line(out_text, wire, line):
    """在所有程序段里找同线号（含注释行）→ 插在其后；否则找同模块最近通道。插入点在 IF 里时挪到 END_IF 之后。"""
    rx = _mod_re(wire)
    ch = int(wire.split('-')[2])
    best = None   # (优先级, 距离, sec, idx, after)
    for sec, lines in out_text.items():
        for i, l in enumerate(lines):
            if l.startswith('//====== ST块'):
                continue
            for m in rx.finditer(l):
                c = int(m.group(1) or (m.group(2) if m.lastindex and m.lastindex > 1 else 0) or 0)
                if c == ch:
                    cand = (0, 0, sec, i, True)
                else:
                    cand = (1, abs(c - ch), sec, i, c < ch)
                if best is None or cand[:2] < best[:2] or (cand[:2] == best[:2] and cand[0] == 0):
                    best = cand
    if best is None:
        return False
    _, _, sec, i, after = best
    lines = out_text[sec]
    dep = _depths(lines)
    pos = i + 1 if after else i
    if dep[min(i, len(dep) - 1)] > 0 or (after and i + 1 < len(dep) and dep[i + 1] > 0 and dep[i] > 0):
        # 在 IF 里面：挪到这个 IF 结构结束之后
        j = i + 1
        while j < len(dep) and dep[j] > 0:
            j += 1
        pos = j          # END_IF 的下一行
    # 不跨过 ST 块分隔标记
    k = pos
    while k > 0 and k <= len(lines) and lines[k - 1].startswith('//====== ST块'):
        k += 1
        break
    lines.insert(min(pos, len(lines)), line)
    return True


def split_block_comments(lines, target_lns):
    """(* ... *) 块里有要启用的行时，把整块改成逐行 // 注释（行数不变），后面按普通注释行启用"""
    if not target_lns:
        return lines
    lines = list(lines)
    i = 0
    while i < len(lines):
        t = lines[i].lstrip()
        if t.startswith('(*'):
            j = i
            while j < len(lines) and '*)' not in lines[j]:
                j += 1
            if j < len(lines) and any(i + 1 <= ln <= j + 1 for ln in target_lns):
                for k in range(i, j + 1):
                    l = lines[k]
                    ind = l[:len(l) - len(l.lstrip())]
                    body = l.strip().replace('(*', '', 1) if k == i else l.strip()
                    if k == j:
                        body = body[::-1].replace(')*', '', 1)[::-1]
                    body = body.strip()
                    lines[k] = (ind + (body if body.startswith('//') else '//' + body)) if body else ''
            i = j + 1
        else:
            i += 1
    return lines


def group_with_old_comment(lines, moved):
    """改了线号的行，如果同一 ST 块里前面有该变量被注释掉的旧行（如“点位损坏”），挪到那行后面，保持编号顺序"""
    lines = list(lines)
    moved_lines = [lines[i] for i, _ in moved]
    for line, (_, v) in zip(moved_lines, moved):
        idx = lines.index(line)          # 每次按内容重新定位（前面的挪动会改变行号）
        j = idx - 1
        target = None
        while j >= 0 and not lines[j].startswith('//====== ST块'):
            l = lines[j].lstrip()
            if l.startswith('//') and re.match(r'/+\s*' + re.escape(v) + r'\s*:=', l) and '新图纸注释' not in l:
                target = j
                break
            j -= 1
        if target is None or target == idx - 1:
            continue
        if _depths(lines)[target] != _depths(lines)[idx]:
            continue          # 不跨 IF 结构
        del lines[idx]
        lines.insert(target + 1, line)
    return lines


TOKEN_RX = re.compile(r'\b(ID|OD)_([A-Z][0-9A-Z]{2,3})(?:_(\d{2})\b|\[(\d+)\])|\b(AD|DA)_([A-Z][0-9A-Z]{2,3})_(\d{1,2})\b')


def line_wire(line):
    """一行里的线号 → 图纸线号格式（I-A01-00 / AD-C05-1），找不到返回 None"""
    m = TOKEN_RX.search(line)
    if not m:
        return None
    if m.group(1):
        return f"{'I' if m.group(1) == 'ID' else 'O'}-{m.group(2)}-{int(m.group(3) or m.group(4)):02d}"
    return f'{m.group(5)}-{m.group(6)}-{int(m.group(7))}'


ARR_RX = re.compile(r'\b(ID|OD)_([A-Z][0-9A-Z]{2,3})\[(\d+)\]')
ANA_RX = re.compile(r'\b(AD|DA)_([A-Z][0-9A-Z]{2,3})_0(\d)\b')


def unify_tokens(line):
    """有效代码部分的线号统一成 I/O 映射写法：ID_E09[0]→ID_E09_00，AD_C05_01→AD_C05_1（注释部分不动）"""
    if line.lstrip().startswith('//'):
        return line
    code, sep, cmt = line.partition('//')
    code = ARR_RX.sub(lambda m: f'{m.group(1)}_{m.group(2)}_{int(m.group(3)):02d}', code)
    code = ANA_RX.sub(lambda m: f'{m.group(1)}_{m.group(2)}_{m.group(3)}', code)
    return code + sep + cmt


def tidy_section(lines, order, active_vars):
    """1) 删掉旧程序里本来就注释掉、且该变量现在已有有效行的旧行（如“点位损坏”）
       2) 每个 ST 块里，连续的顶层赋值行按图纸线号顺序排列（IF 内、空行、说明注释、块注释都作为分隔，不跨越）"""
    lines = [unify_tokens(l) for l in lines]
    out, n_del = [], 0
    for l in lines:
        t = l.lstrip()
        m = re.match(r'/+\s*([^\s:=/]+)\s*:=', t)
        old_wiring = m and m.group(1) in active_vars and line_wire(t) is not None   # 只删带旧线号的；:=TRUE、HMI 等备用写法保留
        if t.startswith('//') and '新图纸注释' not in t and '====== ST块' not in t and old_wiring:
            n_del += 1
            continue
        out.append(l)
    lines = out
    dep = _depths(lines)
    act = _active(lines)
    in_blk = [False] * len(lines)
    blk = False
    for i, l in enumerate(lines):
        if '(*' in l:
            blk = True
        in_blk[i] = blk
        if '*)' in l:
            blk = False
    res, run, n_sorted = [], [], 0

    def flush():
        nonlocal n_sorted
        units = []
        for l in run:
            w = line_wire(l.split('//', 1)[0] if not l.lstrip().startswith('//') else l.lstrip().lstrip('/'))
            if w is None and units:
                units[-1][1].append(l)
            else:
                units.append([order.get(w, None) if w else None, [l]])
        if all(u[0] is not None for u in units) and len(units) > 1:
            new = sorted(units, key=lambda u: u[0])
            if new != units:
                n_sorted += 1
            units = new
        for u in units:
            res.extend(u[1])
        run.clear()

    for i, l in enumerate(lines):
        t = l.strip()
        is_assign = ':=' in t and line_wire(t) is not None
        barrier = (not t or t.startswith('//======') or dep[i] > 0 or in_blk[i]
                   or (act[i] and not is_assign) or (t.startswith('//') and ':=' not in t))
        if barrier:
            flush()
            res.append(l)
        else:
            run.append(l)
    flush()
    return res, n_del, n_sorted


def split_blocks(text):
    """按 //====== ST块 N ====== 标记拆块，返回 {N: 内容}"""
    parts = re.split(r'//====== ST块 (\d+) ======\r?\n', text)
    return {int(parts[i]): parts[i + 1].rstrip('\r\n') for i in range(1, len(parts), 2)}


def write_paste_helper(prog_dir, out_text, path):
    """粘贴助手网页：只列有改动的 ST 块，每块一个复制按钮"""
    import html
    secs = []
    total = 0
    for sec in sorted(out_text):
        old_p = os.path.join(prog_dir, sec + '.st.txt')
        old = split_blocks(open(old_p, encoding='utf-8').read()) if os.path.exists(old_p) else {}
        new = split_blocks('\n'.join(out_text[sec]))
        changed = [(k, v) for k, v in sorted(new.items()) if v.strip() != old.get(k, '').strip()]
        if changed:
            secs.append((sec, len(new), changed))
            total += len(changed)
    parts = []
    for sec, nblk, changed in secs:
        items = []
        for k, v in changed:
            body = v.strip('\r\n')
            marks = ''.join('<span class="m">' + html.escape(m) + '</span>'
                            for m in sorted({'改线号' if '// 原 ' in l else '注释' if '新图纸注释' in l else '新增' if '新增点' in l else ''
                                             for l in body.splitlines()} - {''}))
            items.append(f'''<div class="blk" id="b{len(parts)}_{k}">
  <div class="hd"><b>ST块 {k}</b> {marks}<button onclick="cp(this)">复制</button></div>
  <pre>{html.escape(body)}</pre></div>''')
        parts.append(f'<h2>{html.escape(sec)} <small>共 {nblk} 块，需粘贴 {len(changed)} 块</small></h2>' + '\n'.join(items))
    page = f'''<!DOCTYPE html><html lang="zh"><meta charset="utf-8"><title>程序段粘贴助手</title>
<style>
body{{font-family:"Microsoft YaHei",sans-serif;margin:20px;background:#f6f6f6}}
h1{{font-size:20px}} h2{{font-size:16px;margin-top:28px;border-bottom:2px solid #2f5597}}
small{{color:#666;font-weight:normal}}
.blk{{background:#fff;border:1px solid #ccc;margin:8px 0;border-radius:4px}}
.blk.done{{opacity:.35}}
.hd{{padding:6px 10px;background:#ddebf7;display:flex;gap:8px;align-items:center}}
.hd button{{margin-left:auto;padding:4px 18px;font-size:14px;cursor:pointer}}
.m{{font-size:12px;background:#fff;border:1px solid #999;border-radius:3px;padding:0 4px}}
pre{{margin:0;padding:8px 10px;max-height:160px;overflow:auto;font-size:12px}}
#tip{{position:sticky;top:0;background:#fff3cd;padding:8px;border:1px solid #e0c060}}
</style>
<h1>程序段粘贴助手</h1>
<div id="tip">共 {total} 个 ST 块需要粘贴（没列出的块不用动）。点「复制」→ 在 Sysmac 打开该程序段的第 N 个 ST 块 → Ctrl+A → Ctrl+V。已复制的块会变灰。<span id="cnt"></span></div>
{''.join(parts)}
<script>
function cp(b){{
  const blk=b.closest('.blk'); const t=blk.querySelector('pre').innerText;
  const ta=document.createElement('textarea'); ta.value=t.replace(/\\n/g,'\\r\\n'); document.body.appendChild(ta);
  ta.select(); document.execCommand('copy'); ta.remove();
  blk.classList.add('done'); b.innerText='已复制';
  document.getElementById('cnt').innerText=' 已完成 '+document.querySelectorAll('.blk.done').length+' / {total}';
}}
</script></html>'''
    with open(path, 'w', encoding='utf-8') as f:
        f.write(page)
    return total


def safe_path(path):
    """文件被 Excel 占用时换个名字保存"""
    try:
        if os.path.exists(path):
            open(path, 'a').close()
        return path
    except PermissionError:
        import time
        alt = path.replace('.xlsx', time.strftime('_%H%M%S.xlsx'))
        print(f'⚠ {os.path.basename(path)} 正被打开，另存为 {os.path.basename(alt)}')
        return alt


def write_clip_bat(prog_dir, out_dir):
    """生成本项目的「放入剪贴板.bat」（GBK 编码，中文路径才不会乱码）"""
    writer = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                          '..', 'sysmac-read', 'write_sysmac_clipboard.py'))
    raw = os.path.abspath(os.path.join(prog_dir, 'clipboard_raw.bin'))
    out = os.path.abspath(out_dir)
    bat = f"""@echo off
set PYTHONIOENCODING=gbk:replace
python "{writer}" "{raw}" "{out}" --types "{os.path.join(out, '新增变量.txt')}"
if errorlevel 1 (
  echo.
  echo 【失败】没有放入剪贴板，请把上面的提示截图发给 Claude。
) else (
  echo.
  echo 【成功】现在切到 Sysmac：右键 POUs，选“粘贴多个项目”。中间不要再复制别的东西。
)
pause
"""
    try:
        with open(os.path.join(out, '放入剪贴板.bat'), 'w', encoding='gbk', newline='\r\n') as f:
            f.write(bat)
    except UnicodeEncodeError:
        print('⚠ 路径里有 GBK 不支持的字符，放入剪贴板.bat 未生成；请让 Claude 直接运行 write_sysmac_clipboard.py')


def write_questions(questions, path):
    """需要用户判断的点：每行一个，蓝色两列填写（下拉 + 自由填写）"""
    from openpyxl.worksheet.datavalidation import DataValidation
    wb = Workbook()
    ws = wb.active
    ws.title = '待判断'
    ws.append(['编号', '类别', '线号', '页码', '图纸功能', '现在的情况', '你的处理（下拉）',
               '变量名 / 说明（填写）', '可选处理', '图纸原线号'])
    seen = set()
    blue = PatternFill('solid', fgColor='DDEBF7')
    n = 0
    for cat, r, situ, opts in questions:
        key = (cat, r['wire'], situ)
        if key in seen:
            continue
        seen.add(key)
        n += 1
        ws.append([n, cat, C.prog_name(r['wire']), int(r['page']), r['function'] or '', situ, '', '',
                   opts.replace(',', ' / '), r['wire']])
        ws.cell(ws.max_row, 7).fill = blue
        ws.cell(ws.max_row, 8).fill = blue
        dv = DataValidation(type='list', formula1=f'"{opts}"', allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(ws.cell(ws.max_row, 7))
    for i, wd in enumerate([5, 14, 11, 6, 32, 60, 16, 32, 44, 11], 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = wd
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.freeze_panes = 'C2'
    ws2 = wb.create_sheet('说明')
    for line in [
        '合并(AND)：两个点都接同一个变量，变量 := 点1 AND 点2（常闭急停：任一按下即 FALSE）',
        '合并(OR)：变量 := 点1 OR 点2（常开信号：任一触发即 TRUE）',
        '新建变量：本点新建变量，名字写在「变量名 / 说明」列（不写就按规则起名）',
        '本点用原变量：原变量留在本点，另一处新建变量',
        '换成新变量：该线号改接「变量名 / 说明」列里写的变量',
        '保留原变量：程序行不动',
        '注释掉：程序行加 // 注释',
        '图纸多余 / 改名 / 其他：在「变量名 / 说明」列写清楚',
    ]:
        ws2.append([line])
    ws2.column_dimensions['A'].width = 90
    wb.save(safe_path(path))


def write_new_vars_xlsx(new_vars, plan, plan_row, path):
    """新增变量单独一张表：左边按 Sysmac 全局变量表列顺序（可整块复制粘贴），右边是核对信息"""
    src = {'assign': '你指定的', 'same_as_A': '照机头A改的', 'new': '按规则生成，请过目'}
    wb = Workbook()
    ws = wb.active
    ws.title = '新增变量'
    hdr = ['名称', '数据类型', '初始值', '分配到', '保持', '常量', '网络公开', '注释',
           '', '线号', '页码', '名字来源']
    ws.append(hdr)
    grey = PatternFill('solid', fgColor='EDEDED')
    for v, typ, func in new_vars:
        r = plan_row.get(v) or {'page': 0, 'action': 'io'}
        if v not in plan:
            plan = {**plan, v: ''}
        how = src.get(r.get('action'), '按同类点套用' if '套用' in (r.get('note') or '') else '工具生成')
        if r.get('action') == 'new' and '套用' in (r.get('note') or ''):
            how = '按同类点套用，请过目'
        ws.append([v, typ, '', '', 'False', 'False', '公开', func,
                   '', C.prog_name(plan[v]), int(r['page']), how])
        for col in range(10, 13):
            ws.cell(ws.max_row, col).fill = grey
        if '请过目' in how:
            ws.cell(ws.max_row, 1).fill = PatternFill('solid', fgColor='FFEB9C')
    for i, wd in enumerate([40, 10, 8, 8, 7, 7, 9, 40, 2, 12, 6, 22], 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = wd
    for c in ws[1]:
        c.font = Font(bold=True)
    ws.freeze_panes = 'B2'
    ws2 = wb.create_sheet('说明')
    for line in [
        '最省事：打开 新增变量.txt，全选复制，直接粘贴到全局变量表。A~H 列与它格式相同，也可以从这里复制。',
        '如果你的全局变量表列顺序不同，按表头调整列顺序后再复制；或截图表头发给 Claude 改格式。',
        '黄色名称 = 工具生成的名字，请过目；末尾带 _模块号通道号（如 _H9D06）的是重名后自动加的后缀。',
        'J~L 列只用于核对（对应线号、图纸页码、名字来源），不要一起复制。',
        '数据类型：数字量 BOOL；模拟量直接读取用 INT（若程序里要换算成 REAL，自行改类型）。',
    ]:
        ws2.append([line])
    ws2.column_dimensions['A'].width = 100
    wb.save(safe_path(path))


def main():
    csv_path, prog_dir, out_dir = sys.argv[1:4]
    fb_path = sys.argv[4] if len(sys.argv) > 4 else os.path.join(os.path.dirname(os.path.abspath(csv_path)), '对点反馈.json')
    feedback = json.load(open(fb_path, encoding='utf-8')) if os.path.exists(fb_path) else {}
    drawing = read_points_csv(csv_path)
    if not drawing or not any(d.get('wire') for d in drawing):
        sys.exit('图纸点表没有有效线号，已停止生成，原有结果未覆盖。')
    try:
        C.check_unsupported_program(prog_dir)
    except ValueError as e:
        sys.exit(str(e))
    program = C.parse_program(prog_dir)
    if not program:
        sys.exit('旧程序没有识别出 I/O 映射行，已停止生成，原有结果未覆盖；请检查线号格式。')
    results, extra = C.compare(drawing, program, feedback)

    array_mods = {('ID' if p['wire'][0] == 'I' else 'OD', p['wire'].split('-')[1])
                  for p in program if p.get('array')}
    declared = set(re.findall(r'N=([^\t\r\n]+)\t', open(os.path.join(prog_dir, 'variables.txt'), encoding='utf-8').read())) \
        if os.path.exists(os.path.join(prog_dir, 'variables.txt')) else set()

    # ---------- 1. 迁移计划：变量 → 新线号 ----------
    # 优先级：你明确判断的(3) > 工具自动判断(2) > 工具生成的新名字(1)
    known = {p['var'] for p in program} | declared

    def prio(r):
        act = r.get('action')
        if act in ('assign', 'confirm', 'keep', 'follow_new'):
            return 3
        if act == 'same_as_A' and r.get('new_var') in known:
            return 3   # 机头A 侧是把现成变量挪过来，机头B 照做
        if act in ('new', 'same_as_A') or r['status'].startswith('🆕'):
            return 1
        return 2

    def kind_ok(v, w):
        pv = v.split('_', 1)[0]
        if pv not in ('ID', 'OD', 'AD', 'DA'):
            return True
        return {'I': 'ID', 'O': 'OD'}.get(w.split('-')[0], w.split('-')[0]) == pv

    plan, plan_row, conflicts, orphans = {}, {}, [], []
    multi_out = set()
    multi_in = set()
    old_wire = {p['var']: p['wire'] for p in program if p['active']}
    questions = []
    by_wire = {}
    for r in results:
        if r['wire'] and not r['void']:
            by_wire.setdefault(r['wire'], r)
    known = {p['var'] for p in program} | declared
    for r in sorted(results, key=lambda r: -prio(r)):
        v = final_var(r)
        if not v:
            continue
        if not kind_ok(v, r['wire']):
            conflicts.append(f'{v} 是{v[:2]}变量，但 {C.prog_name(r["wire"])} 是{r["wire"].split("-")[0]}点，类型不符，未应用')
            questions.append(('变量类型不符', r, f'你给的是 {v}（{v[:2]}变量），但这个点是{r["wire"].split("-")[0]}点，未应用',
                              '新建变量,注释掉,其他'))
            continue
        if v in plan and plan[v] != r['wire'] and r['wire'].split('-')[0] in ('O', 'DA') \
                and plan[v].split('-')[0] in ('O', 'DA'):
            multi_out.add((v, r['wire']))      # 输出：同一变量驱动多个输出是允许的
            continue
        if v in plan and plan[v] != r['wire'] and \
                norm_func(r['function']) and norm_func(r['function']) == norm_func(plan_row[v]['function']):
            # 图纸为准：同一功能画在两处，变量保持接在旧程序原来的线号上，另一处列为图纸疑点
            keep_new = old_wire.get(v) == r['wire']
            loser = plan_row[v] if keep_new else r
            if keep_new:
                plan[v], plan_row[v] = r['wire'], r
            if old_wire.get(v) in (plan[v], r['wire']):
                why = f'变量 {v} 保持接在旧程序原来的 {C.prog_name(plan[v])}，本点暂无变量，请核对图纸'
            else:
                why = (f'你给两处都指定了 {v}（旧程序里它在 {C.prog_name(old_wire.get(v, "")) if v in old_wire else "别处"}），'
                       f'暂用 {C.prog_name(plan[v])}，本点暂无变量，请确定用哪一处')
            questions.append(('图纸疑点：同一功能画在两处', loser,
                              f'图纸上 {C.prog_name(plan[v])}（第{plan_row[v]["page"]}页）和本点功能相同；' + why,
                              '图纸多余,新建变量,本点用原变量,其他'))
            conflicts.append(f'{v}：图纸上 {C.prog_name(plan[v])} 和 {C.prog_name(loser["wire"])} 功能相同，按图纸为准保持原线号')
            continue
        if v in plan and plan[v] != r['wire']:
            if prio(r) == 1:
                _, m_, c_ = r['wire'].split('-')
                nv = f'{v}_{m_}{int(c_):02d}'
                conflicts.append(f'生成的名字 {v} 已被 {C.prog_name(plan[v])} 使用，{C.prog_name(r["wire"])} 暂用 {nv}，请改名')
                questions.append(('生成的名字重名', r, f'套用出来的名字 {v} 已被 {C.prog_name(plan[v])} 使用，暂用 {nv}；也可能是图纸重复画了',
                                  '改名,图纸多余,其他'))
                v = nv
            else:
                orphans.append(r)
                questions.append(('同类点出现两处', r,
                                  f'旧变量 {v} 已接到 {C.prog_name(plan[v])}（{plan_row[v]["function"]}），本点现在没有变量',
                                  '合并(AND),合并(OR),新建变量,本点用原变量,注释掉,图纸多余,其他'))
                conflicts.append(f'{v} 同时对应 {C.prog_name(plan[v])} 和 {C.prog_name(r["wire"])}，'
                                 f'已按优先级用 {C.prog_name(plan[v])}；{C.prog_name(r["wire"])}（{r["function"]}）现在没有变量，需要定')
                continue
        elif prio(r) == 1 and v in known and v not in plan:
            # 生成的名字撞上旧程序里的现成变量：只有旧变量确实迁到这里才算
            pass
        plan[v], plan_row[v] = r['wire'], r
    wire_owner = {}
    for v_, w_ in plan.items():
        if w_ in wire_owner and w_.split('-')[0] in ('I', 'AD'):
            r_ = plan_row[v_]
            questions.append(('同一输入点分给两个变量', r_,
                              f'{C.prog_name(w_)} 同时接 {wire_owner[w_]} 和 {v_}，两行都保留，请决定留哪个',
                              '保留原变量,换成新变量,注释掉,其他'))
            multi_in.add(w_)
            continue
        wire_owner[w_] = v_

    # ---------- 2. 逐行改写 ----------
    idx = {(p['section'], p['line']): p for p in program}
    changes, review = [], []
    written_vars = set()
    written_out = set()
    out_text = {}
    files = sorted(f for f in os.listdir(prog_dir) if f.endswith('.st.txt'))
    for fn in files:
        sec = fn[:-len('.st.txt')]
        lines = open(os.path.join(prog_dir, fn), encoding='utf-8').read().splitlines()
        lines = split_block_comments(lines, {plan_row[v]['line'] for v in plan
                                             if plan_row[v].get('action') == 'enable'
                                             and plan_row[v].get('section') == sec})
        new_lines = []
        moved_here = []
        for ln, raw in enumerate(lines, 1):
            p = idx.get((sec, ln))
            if p and not p['active'] and raw.lstrip().startswith('//') and plan.get(p['var']) == p['wire'] \
                    and plan_row[p['var']].get('action') == 'enable' and p['var'] not in written_vars:
                line = re.sub(r'^(\s*)/+\s*', r'\1', raw).rstrip() + '   // 图纸有此点，已启用（原程序注释）'
                new_lines.append(line)
                written_vars.add(p['var'])
                changes.append((sec, ln, '启用', raw.strip(), line.strip(), C.prog_name(p['wire'])))
                continue
            if not p or not p['active']:
                new_lines.append(raw)
                continue
            v, w = p['var'], p['wire']
            if (v, w) in multi_out:
                new_lines.append(raw)
                written_out.add((v, w))
                continue
            if v in plan:
                nw = plan[v]
                written_vars.add(v)
                if nw == w:
                    new_lines.append(raw)
                    continue
                ot = old_token(raw, w)
                nt = token(nw, array_mods)
                if not ot:
                    new_lines.append(raw)
                    review.append((sec, ln, raw, f'要改到 {nt}，但没找到原线号写法，请手改'))
                    continue
                code, _, cmt = raw.partition('//')
                line = code.replace(ot, nt, 1).rstrip() + f'   // 原 {ot}' + (f'  {cmt.strip()}' if cmt.strip() else '')
                new_lines.append(line)
                moved_here.append((len(new_lines) - 1, v))
                changes.append((sec, ln, '改线号', raw.strip(), line.strip(), C.prog_name(nw) + ' ' + (plan_row[v]['function'] or '')))
                continue
            d = by_wire.get(w)
            if d is None:
                reason = '新图纸上没有这个线号'
            elif wire_owner.get(w) and wire_owner[w] != v:
                reason = f'新图纸该点改给 {wire_owner[w]}'
            elif d['status'].startswith(DROP_PREFIX) or d['void']:
                reason = f'新图纸：{d["status"].lstrip("✅🛠 ")}'
            else:
                new_lines.append(raw)
                review.append((sec, ln, raw.strip(), f'{d["status"]}：{d["function"] or ""}'))
                questions.append(('新图纸功能变了', d, f'程序第{ln}行（{sec}）：{raw.strip()}',
                                  '保留原变量,换成新变量,注释掉,其他'))
                continue
            line = '//' + raw.rstrip() + f'   // 新图纸注释：{reason}'
            new_lines.append(line)
            changes.append((sec, ln, '注释掉', raw.strip(), line.strip(), reason))
        out_text[sec] = group_with_old_comment(new_lines, moved_here)

    # ---------- 3. 新增行：放到同模块所在的程序段 ----------
    mod_sec = defaultdict(Counter)
    area_sec = defaultdict(Counter)
    for p in program:
        k, mod, _ = p['wire'].split('-')
        io = 'in' if k in ('I', 'AD') else 'out'
        target = plan.get(p['var'], p['wire'])
        mod_sec[(io, target.split('-')[1])][p['section']] += 1
        area_sec[(io, mod[0])][p['section']] += 1
    adds = defaultdict(list)
    for v, w in sorted(plan.items(), key=lambda x: x[1]):
        if v in written_vars:
            continue
        k, mod, ch = w.split('-')
        io = 'in' if k in ('I', 'AD') else 'out'
        sec = (mod_sec.get((io, mod)) or area_sec.get((io, mod[0])) or Counter()).most_common(1)
        sec = sec[0][0] if sec else ('新增_Input刷新' if io == 'in' else '新增_Output刷新')
        t = token(w, array_mods)
        line = f'{v}:={t};' if io == 'in' else f'{t}:={v};'
        # 变量在旧程序里出现过（含已注释的行）→ 沿用原写法（如 INT_TO_REAL(...)/8），只换线号
        tpl = next((p for p in program if p['var'] == v and p['wire'].split('-')[0] == k), None)
        if tpl:
            code = re.sub(r'^\s*/+\s*', '', tpl['raw']).split('//')[0].strip()
            ot = old_token(code, tpl['wire'])
            if ot and code.endswith(';'):
                line = code.replace(ot, t, 1)
        func = plan_row[v]['function'] or ''
        adds[sec].append((w, f'{line}   // {func}'))
        changes.append((sec, '', '新增', '', line, f'{C.prog_name(w)} {func}'))
    for v, w in sorted(multi_out - written_out, key=lambda x: x[1]):
        k, mod, ch = w.split('-')
        sec = (mod_sec.get(('out', mod)) or area_sec.get(('out', mod[0])) or Counter()).most_common(1)
        sec = sec[0][0] if sec else '新增_Output刷新'
        t = token(w, array_mods)
        adds[sec].append((w, f'{t}:={v};   // 同一变量驱动多个输出'))
        changes.append((sec, '', '新增', '', f'{t}:={v};', C.prog_name(w)))
    # 新增行放到同线号的旧行旁边；没有同线号就放在同模块相邻通道旁边；都没有才放段末
    tail = defaultdict(list)
    for sec, items in adds.items():
        for w, line in items:
            if not place_line(out_text, w, line + '   // 新增'):
                tail[sec].append(line)
    for sec, ls in tail.items():
        out_text.setdefault(sec, [])
        out_text[sec] += ['', '//---------- 新增点（对点工具生成） ----------'] + ls

    # ---------- 3.4 变量已接图纸输入点，别处又用非线号表达式给它赋值 → 注释那行 ----------
    in_vars = {v: w for v, w in plan.items() if w.split('-')[0] in ('I', 'AD')}
    for sec, ls in out_text.items():
        act = _active(ls)
        for i, (l, a) in enumerate(zip(ls, act)):
            m = re.match(r'^([^\s:=]+)\s*:=\s*(.*);', a)
            if not m or m.group(1) not in in_vars or line_wire(a):
                continue
            if _depths(ls)[i] > 0:
                questions.append(('变量另有赋值（IF 内，未自动处理）', plan_row[m.group(1)],
                                  f'{sec}：{l.strip()}；该变量已接 {C.prog_name(in_vars[m.group(1)])}', '注释掉,保留,其他'))
                continue
            new = '//' + l.rstrip() + f'   // 新图纸注释：该变量改由图纸输入 {C.prog_name(in_vars[m.group(1)])} 赋值'
            ls[i] = new
            changes.append((sec, '', '注释掉', l.strip(), new.strip(), f'{m.group(1)} 已接 {C.prog_name(in_vars[m.group(1)])}'))

    # ---------- 3.5 编译检查用的后处理：空 IF 整体注释、空块加占位语句 ----------
    fix_stats = {'空IF注释': 0, '空块占位': 0}
    for sec in out_text:
        out_text[sec] = postprocess_section(out_text[sec], fix_stats)
    if any(fix_stats.values()):
        print(f'编译修正：空 IF 结构注释 {fix_stats["空IF注释"]} 处，空 ST 块加占位语句 {fix_stats["空块占位"]} 个')

    # ---------- 3.6 整理：删掉无用的旧注释行、按图纸线号顺序排列 ----------
    order = {}
    for i, d in enumerate(sorted((d for d in drawing if d['wire'] and d['void'] != '整页作废'),
                                 key=lambda d: (int(d['page']), d['wire']))):
        order.setdefault(d['wire'], i)
    active_vars = set(plan) | {v for v, _ in multi_out}
    n_del = n_sorted = 0
    for sec in out_text:
        out_text[sec], a_, b_ = tidy_section(out_text[sec], order, active_vars)
        n_del += a_
        n_sorted += b_
    print(f'整理：删除无用旧注释行 {n_del} 行，按图纸顺序重排 {n_sorted} 处')

    # ---------- 4. 输出 ----------
    os.makedirs(out_dir, exist_ok=True)
    for sec, ls in out_text.items():
        with open(os.path.join(out_dir, sec + '.st.txt'), 'w', encoding='utf-8', newline='\r\n') as f:
            f.write('\n'.join(ls))

    n_paste = write_paste_helper(prog_dir, out_text, os.path.join(out_dir, '粘贴助手.html'))
    write_clip_bat(prog_dir, out_dir)
    print(f'需要粘贴的 ST 块 {n_paste} 个 → 粘贴助手.html')

    new_vars = []
    for v, w in sorted(plan.items(), key=lambda x: x[1]):
        base = v.split('[')[0]
        if base in declared or v in declared:
            continue
        typ = 'BOOL' if w[0] in 'IO' else 'INT'
        new_vars.append((v, typ, plan_row[v]['function'] or ''))
    for v, w in sorted(plan.items(), key=lambda x: x[1]):
        r = plan_row[v]
        if '仅在现场接线页' in (r.get('note') or '') or '仅在现场接线页' in (r.get('drawing_note') or ''):
            t = token(w, array_mods)
            if t not in declared and all(t != x[0] for x in new_vars):
                new_vars.append((t, 'BOOL' if w[0] in 'IO' else 'INT', f'图纸无此模块I/O表（第{r["page"]}页），先建为普通全局变量'))
    # Sysmac 全局变量表粘贴格式：名称 类型 初始值 分配到 保持 常量 网络公开 注释（Tab 分隔，UTF-8 无BOM，CRLF）
    with open(os.path.join(out_dir, '新增变量.txt'), 'w', encoding='utf-8', newline='\r\n') as f:
        f.write('\n'.join(f'{v}\t{t}\t\t\tFalse\tFalse\t公开\t{c}' for v, t, c in new_vars) + '\n')
    write_new_vars_xlsx(new_vars, plan, plan_row, os.path.join(out_dir, '新增变量.xlsx'))

    wb = Workbook()
    ws = wb.active
    ws.title = '变更清单'
    ws.append(['程序段', '原行号', '动作', '原文', '新文', '依据（新图纸）'])
    fills = {'改线号': 'BDD7EE', '注释掉': 'FCE4D6', '新增': 'E2EFDA', '启用': 'FFF2CC'}
    for c in changes:
        ws.append(list(c))
        ws.cell(ws.max_row, 3).fill = PatternFill('solid', fgColor=fills[c[2]])
    for i, wd in enumerate([22, 7, 8, 50, 60, 40], 1):
        ws.column_dimensions[ws.cell(1, i).column_letter].width = wd
    ws2 = wb.create_sheet('需人工确认')
    ws2.append(['程序段', '行', '原文', '原因'])
    for r in review:
        ws2.append(list(r))
    for msg in conflicts:
        ws2.append(['', '', '', msg])
    for i, wd in enumerate([22, 6, 60, 60], 1):
        ws2.column_dimensions[ws2.cell(1, i).column_letter].width = wd
    ws3 = wb.create_sheet('新增变量')
    ws3.append(['名称', '数据类型', '注释'])
    for x in new_vars:
        ws3.append(list(x))
    for ws_ in wb.worksheets:
        for c in ws_[1]:
            c.font = Font(bold=True)
    wb.save(safe_path(os.path.join(out_dir, '变更清单.xlsx')))
    write_questions(questions, os.path.join(out_dir, '待判断.xlsx'))

    cnt = Counter(c[2] for c in changes)
    print(f'程序段 {len(out_text)} 个 → {os.path.abspath(out_dir)}')
    print(f'改线号 {cnt["改线号"]}，注释掉 {cnt["注释掉"]}，新增 {cnt["新增"]}，需人工确认 {len(review) + len(conflicts)}，新建变量 {len(new_vars)}')


if __name__ == '__main__':
    main()
