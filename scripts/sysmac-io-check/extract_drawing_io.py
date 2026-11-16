"""从 CAD 导出的 PDF 电气图中提取 I/O 点表（线号 / 类别 / 功能 / 元件标号 / 页码）。

用法: python extract_drawing_io.py <图纸.pdf> <输出.csv>

原理: I/O 接线图页底部有「端子 / 类别 / 功能 / 型号」表格，
每个点有 INPUTn/OUTPUTn 标签和线号（如 I-A01-00）。
按 INPUT/OUTPUT 标签划分每个点的槽位，再把同一槽位里的功能文字、线号、元件标号归到一起。
"""
import csv
import re
import sys

import pymupdf

WIRE = re.compile(r'^([IO])[-_]([A-Z])(\d[0-9A-Z]|\d{3})-(\d{2})$')   # 模块号可带字母: E5C
SLOT = re.compile(r'^(INPUT|OUTPUT)(\d+)$')
DEVICE = re.compile(r'^[A-Z]{1,4}\d{2,4}[A-Z]?(:[A-Z0-9]+)?$')
PLC_LABEL = re.compile(r'^PLC(\d{2})-(\d{2})(?:-(\d))?$')   # PLC11-05-2 = 8通道模块第2页(CH5~8)
CH = re.compile(r'^CH(\d)$')
ANA_WIRE = re.compile(r'^([AD])-([A-Z])(\d[0-9A-Z]|\d{3})-(\d{1,2})[+-]?$')   # A-C05-1+ / D-E54-1+
IO_SLOT = re.compile(r'^(INPUT|OUTPUT)\d+$')


def norm_wire_text(t):
    """返回 (规范线号, 是否书写不规范)。老式与机构代号式统一由 parse_address 判定。"""
    from io_address import parse_address as _pa
    a = _pa(t)
    if a and a.scheme == 'org':
        # 机构代号式：归一成程序写法 B_I1_00 / A_AI2_4，图纸的连字符/前导零都视为书写差异
        return a.program_token(), ('-' in t or re.search(r'0\d', t.split('-')[-1]) is not None)
    m = WIRE.match(t)
    if not m:
        return '', False
    return f'{m.group(1)}-{m.group(2)}{m.group(3)}-{m.group(4)}', '_' in t


def center(w):
    return ((w[0] + w[2]) / 2, (w[1] + w[3]) / 2)


def page_points(page, pno, words=None):
    words = words or page.get_text('words')
    slots = [w for w in words if SLOT.match(w[4])]
    from io_address import parse_address as _pa
    wires = [w for w in words if WIRE.match(w[4]) or
             (lambda a: a and a.scheme == 'org')(_pa(w[4]))]
    if len(slots) < 4 or not wires:
        return []
    hdr = {w[4]: w for w in words if w[4] in ('功能', '类别', '端子', '型号')}
    if '功能' not in hdr:
        return []

    # 判断方向: 槽位沿 y 排列（图纸旋转 90°）还是沿 x 排列
    ys = [center(s)[1] for s in slots]
    xs = [center(s)[0] for s in slots]
    along_y = (max(ys) - min(ys)) > (max(xs) - min(xs))
    a = 1 if along_y else 0      # 槽位排列轴
    b = 0 if along_y else 1      # 表格行轴（功能/类别/端子 所在轴）

    title = ''
    for w in words:
        if '接线图' in w[4] or 'I/O' in w[4]:
            title = w[4]
            break

    func_c = center(hdr['功能'])[b]
    cat_c = center(slots[0])[b]
    # 功能行的范围: 从功能表头向远离"类别"行的方向 ~ 到类别行之间
    sign = 1 if cat_c > func_c else -1
    func_lo = func_c - sign * 15
    func_hi = func_c + (cat_c - func_c) * 0.6

    def in_func_band(w):
        v = center(w)[b]
        lo, hi = sorted((func_lo, func_hi))
        return lo <= v <= hi

    slots.sort(key=lambda s: center(s)[a])
    cents = [center(s)[a] for s in slots]
    bounds = []
    for i, c in enumerate(cents):
        lo = (cents[i - 1] + c) / 2 if i else c - (cents[1] - c) / 2
        hi = (c + cents[i + 1]) / 2 if i + 1 < len(cents) else c + (c - cents[i - 1]) / 2
        bounds.append((lo, hi))

    func_words = [w for w in words if in_func_band(w) and w[4] not in ('功能',)]

    # 功能格里画了大叉 = 作废点：找跨度较大的对角线，取其中点
    crosses = []
    for d in page.get_drawings():
        for it in d['items']:
            if it[0] != 'l':
                continue
            p1, p2 = it[1], it[2]
            dx, dy = abs(p1.x - p2.x), abs(p1.y - p2.y)
            if 20 < dx < 120 and 20 < dy < 120:
                mid = ((p1.x + p2.x) / 2, (p1.y + p2.y) / 2)
                lo_, hi_ = sorted((func_lo, func_hi))
                if lo_ - 20 <= mid[b] <= hi_ + 20:
                    crosses.append(mid)
    used = set()
    pts = []
    for s, (lo, hi) in zip(slots, bounds):
        inslot = lambda w: lo <= center(w)[a] < hi
        wire = [w for w in wires if inslot(w)]
        fw = [w for w in func_words if inslot(w)]
        # 竖排文字: 每列 x 从大到小为阅读顺序；横排: y 从小到大
        if along_y:
            fw.sort(key=lambda w: (-round(center(w)[0]), center(w)[1]))
        else:
            fw.sort(key=lambda w: (round(center(w)[1]), center(w)[0]))
        func = ''.join(w[4] for w in fw)
        wire_txt, typo = norm_wire_text(wire[0][4]) if wire else ('', False)
        dev = ''
        if wire:
            wc = center(wire[0])
            devs = [w for w in words if DEVICE.match(w[4]) and inslot(w)
                    and sign * (center(w)[b] - wc[b]) > 0]
            if devs:
                devs.sort(key=lambda w: abs(center(w)[b] - wc[b]))
                dev = devs[0][4]
        void = sum(1 for c in crosses if lo <= c[a] < hi) >= 2
        pts.append({
            'page': pno + 1, 'title': title, 'slot': s[4],
            'wire': wire_txt, 'function': func, 'device': dev,
            'void': '作废' if void else '',
            'note': f'图纸线号书写为 {wire[0][4]}' if typo else '',
        })
        used.update(id(w) for w in wire)
    return pts


def analog_points(page, pno, words, label_map):
    """模拟量页：无线号，用 PLC 标签(PLCxx-yy) + CH0~3 推出线号 AD/DA_区模块_通道。"""
    """模拟量页：优先读图上的模拟量线号 A-C05-1+ / D-E54-1+；
    读不到时用 PLC 标签(PLCxx-yy[-k]) + CHn 推算（-2 表示 8 通道模块的 CH5~8）。"""
    ana = [w for w in words if ANA_WIRE.match(w[4])]
    chs = [w for w in words if CH.match(w[4])]
    if len(chs) < 2 and ana:
        chs = [w for w in words if IO_SLOT.match(w[4])]   # NX 模块表格用 OUTPUT1/2
    mod = [w for w in words if re.fullmatch(r'GL20-\d(AD|DA)|NX-(AD|DA)\w+', w[4])]
    labels = [w for w in words if PLC_LABEL.match(w[4])]
    hdr = [w for w in words if w[4] == '功能']
    if len(chs) < 2 or not (mod or ana) or not hdr:
        return []
    if mod:
        kind = 'AD' if 'AD' in mod[0][4] else 'DA'
    else:
        kind = 'AD' if ANA_WIRE.match(ana[0][4]).group(1) == 'A' else 'DA'
    wire_mod, page_ofs, lab_txt = '', 0, labels[0][4] if labels else ''
    if labels:
        lab = PLC_LABEL.match(lab_txt)
        rule = label_map.get(lab.group(1))
        wire_mod = f'{rule[0]}{int(lab.group(2)) + rule[1]:02d}' if rule else ''
        page_ofs = (int(lab.group(3)) - 1) * 4 if lab.group(3) else 0

    ys = [center(c)[1] for c in chs]
    xs = [center(c)[0] for c in chs]
    along_y = (max(ys) - min(ys)) > (max(xs) - min(xs))
    a = 1 if along_y else 0
    b = 0 if along_y else 1
    func_c = center(hdr[0])[b]
    cat_c = center(chs[0])[b]
    lo_b, hi_b = sorted((func_c - (cat_c - func_c) * 0.4, func_c + (cat_c - func_c) * 0.7))
    chs.sort(key=lambda w: center(w)[a])
    cents = [center(c)[a] for c in chs]
    step = (cents[-1] - cents[0]) / max(len(cents) - 1, 1)
    title = next((w[4] for w in words if '接线图' in w[4] or 'I/O' in w[4]), '')
    pts = []
    for i, (c, cc) in enumerate(zip(chs, cents)):
        lo, hi = cc - step / 2, cc + step / 2
        fw = [w for w in words if lo <= center(w)[a] < hi and lo_b <= center(w)[b] <= hi_b
              and w[4] not in ('功能',) and not CH.match(w[4]) and not IO_SLOT.match(w[4])
              and not ANA_WIRE.match(w[4])]
        if along_y:
            fw.sort(key=lambda w: (-round(center(w)[0]), center(w)[1]))
        else:
            fw.sort(key=lambda w: (round(center(w)[1]), center(w)[0]))
        func = ''.join(w[4] for w in fw if w[4] not in ('*', 'NC'))
        # 1) 图上直接写的线号
        aw = sorted((w for w in ana if lo <= center(w)[a] < hi), key=lambda w: not w[4].endswith('+'))
        note = ''
        if aw:
            m = ANA_WIRE.match(aw[0][4])
            wire = f"{'AD' if m.group(1) == 'A' else 'DA'}-{m.group(2)}{m.group(3)}-{int(m.group(4))}"
        elif wire_mod:
            m = CH.match(c[4])
            n = (int(m.group(1)) if m else i) + 1 + page_ofs   # CH0 → 通道1
            wire = f'{kind}-{wire_mod}-{n}'
            note = f'图上没写线号，由 {lab_txt} {c[4]} 推算，请核对'
        else:
            wire = ''
            note = f'无线号，且无法由 {lab_txt or "模块标签"} 推算'
        pts.append({
            'page': pno + 1, 'title': title, 'slot': f'{lab_txt} {c[4]}'.strip(),
            'wire': wire, 'function': func, 'device': '', 'void': '', 'note': note,
        })
    return pts


def build_label_map(pages_words):
    """从数字量页学习 PLC 标签前缀 → (线号区字母, 模块号偏移)。
    如 PLC30-03 → C03（偏移0）；PLC41-02 → D52（偏移50，扩展站）。"""
    from collections import Counter, defaultdict
    votes = defaultdict(Counter)
    for words in pages_words:
        labs = {w[4] for w in words if PLC_LABEL.match(w[4])}
        wires = {norm_wire_text(w[4])[0][2:5] for w in words if WIRE.match(w[4])}
        if len(labs) == 1 and len(wires) == 1:
            lab = PLC_LABEL.match(labs.pop())
            wm = wires.pop()
            if not wm[1:].isdigit():
                continue   # 带字母的模块号（E5C）不参与推算规则学习
            votes[lab.group(1)][(wm[0], int(wm[1:]) - int(lab.group(2)))] += 1
    return {k: v.most_common(1)[0][0] for k, v in votes.items()}


def prog_name(wire, array_mods=()):
    """图纸线号 → 程序变量名：I-A01-00→ID_A01_00（数组写法模块→ID_E01[0]），AD-C03-1→AD_C03_1"""
    if not wire:
        return ''
    k, mod, ch = wire.split('-')
    if k in ('AD', 'DA'):
        return f'{k}_{mod}_{int(ch)}'
    pre = 'ID' if k == 'I' else 'OD'
    if (pre, mod) in array_mods:
        return f'{pre}_{mod}[{int(ch)}]'
    return f'{pre}_{mod}_{int(ch):02d}'


def page_crossed(page):
    """整页打叉：两条跨越大半个页面的对角线"""
    W, H = page.rect.width, page.rect.height
    n = 0
    for d in page.get_drawings():
        for it in d['items']:
            if it[0] == 'l':
                a, b = it[1], it[2]
                if abs(a.x - b.x) > W * 0.6 and abs(a.y - b.y) > H * 0.6:
                    n += 1
    return n >= 2


def page_module(words):
    m = [w[4] for w in words if re.fullmatch(r'(GL20|NX)-[A-Z0-9]+', w[4])]
    return m[0] if len(set(m)) == 1 else ''


def extract(pdf):
    doc = pymupdf.open(pdf)
    pages_words = [p.get_text('words') for p in doc]
    label_map = build_label_map(pages_words)
    out = []
    for pno, page in enumerate(doc):
        pts = page_points(page, pno, pages_words[pno])
        if not pts:
            pts = analog_points(page, pno, pages_words[pno], label_map)
        if pts:
            crossed = page_crossed(page)
            mod = page_module(pages_words[pno])
            for p_ in pts:
                p_['module'] = mod
                if crossed:
                    p_['void'] = '整页作废'
        out.extend(pts)

    # 只在现场接线页出现、没有 I/O 表的模拟量线号：补进点表，功能取最近的中文说明
    have = {p['wire'] for p in out}
    extra = {}
    for pno, words in enumerate(pages_words):
        cn = [w for w in words if re.search(r'变送器|传感器|压力|液位|温度|流量|电位器|比例阀|气压|浓度', w[4])]
        for w in words:
            m = ANA_WIRE.match(w[4])
            if not m:
                continue
            wire = f"{'AD' if m.group(1) == 'A' else 'DA'}-{m.group(2)}{m.group(3)}-{int(m.group(4))}"
            if wire in have or wire in extra:
                continue
            wc = center(w)
            near = sorted(cn, key=lambda t: (center(t)[0] - wc[0]) ** 2 + (center(t)[1] - wc[1]) ** 2)
            extra[wire] = {
                'page': pno + 1, 'title': '', 'slot': '', 'wire': wire,
                'function': near[0][4] if near else '', 'device': '', 'void': '',
                'module': '', 'note': '图纸无该模块的I/O表，仅在现场接线页出现；功能取附近文字，请核对',
            }
    out.extend(extra.values())
    fix_station_area(out)
    return out


STATIONS = [('A面涂布', '机头A'), ('B面涂布', '机头B'), ('A面牵引', '出料A'), ('B面牵引', '出料B'),
            ('机头A', '机头A'), ('机头B', '机头B'), ('出料A', '出料A'), ('出料B', '出料B')]


def station_of(func):
    for k, v in STATIONS:
        if k in (func or ''):
            return v
    return ''


def fix_station_area(pts):
    """同一线号出现在两页（如第68页机头A误写成 O-F02），按工位所属线号区更正。
    工位→线号区从全图统计：机头A 多数在 B 区，机头B 在 F 区……"""
    from collections import Counter, defaultdict
    vote = defaultdict(Counter)
    for p_ in pts:
        st = station_of(p_['function'])
        if st and p_['wire'] and p_['void'] != '整页作废':
            vote[st][p_['wire'].split('-')[1][0]] += 1
    st_area = {st: c.most_common(1)[0][0] for st, c in vote.items()}
    by_wire = defaultdict(list)
    for p_ in pts:
        if p_['wire'] and p_['void'] != '整页作废':
            by_wire[p_['wire']].append(p_)
    dup_pages = set()
    for w, rows in by_wire.items():
        if len({r['page'] for r in rows}) > 1:
            dup_pages.update(r['page'] for r in rows)
    for pg in dup_pages:
        rows = [x for x in pts if x['page'] == pg and x['wire']]
        sts = Counter(station_of(r['function']) for r in rows if station_of(r['function']))
        if not sts:
            continue
        st = sts.most_common(1)[0][0]
        area = st_area.get(st)
        if not area:
            continue
        for r in rows:
            k, mod, ch = r['wire'].split('-')
            if mod[0] != area:
                new = f'{k}-{area}{mod[1:]}-{ch}'
                r['note'] = (r['note'] + '；' if r['note'] else '') + \
                    f'图纸线号写成 {r["wire"]}，本页是{st}，已按{st}所在的 {area} 区更正为 {new}'
                r['wire'] = new


# 点表 CSV：中文表头，线号用程序写法 ID_A01_00，按模块排序、模块之间空一行
CSV_COLS = [('page', '页码'), ('title', '图纸标题'), ('module', '模块型号'), ('slot', '槽位'),
            ('prog_wire', '线号'), ('function', '功能'), ('device', '元件'), ('void', '作废'),
            ('note', '说明'), ('wire', '图纸原线号')]


def write_points_csv(pts, path):
    for p_ in pts:
        p_['prog_wire'] = prog_name(p_['wire'])
    groups = {}
    for p_ in pts:
        key = p_['wire'].rsplit('-', 1)[0] if p_['wire'] else f"p{p_['page']}"
        groups.setdefault(key, []).append(p_)
    order = sorted(groups, key=lambda k: min(int(p_['page']) for p_ in groups[k]))
    with open(path, 'w', newline='', encoding='utf-8-sig') as f:
        wr = csv.writer(f)
        wr.writerow([c for _, c in CSV_COLS])
        for i, k in enumerate(order):
            if i:
                wr.writerow([])
            rows = sorted(groups[k], key=lambda p_: int(p_['wire'].rsplit('-', 1)[1]) if p_['wire'] else 0)
            for p_ in rows:
                wr.writerow([p_.get(key, '') for key, _ in CSV_COLS])


def read_points_csv(path):
    """读点表 CSV（兼容新旧格式），跳过空行，返回内部字段名"""
    rows = []
    with open(path, encoding='utf-8-sig') as f:
        rd = csv.DictReader(f)
        cn2key = {c: k for k, c in CSV_COLS}
        for r in rd:
            if not any((v or '').strip() for v in r.values()):
                continue
            rows.append({cn2key.get(k, k): (v or '') for k, v in r.items()})
    return rows


def main():
    pdf, csv_path = sys.argv[1], sys.argv[2]
    pts = extract(pdf)
    if not pts or not any(p.get('wire') for p in pts):
        sys.exit('图纸未识别到有效 I/O 点位：请检查图纸是否包含受支持格式的 I/O 表和线号；已停止，未覆盖点表。')
    write_points_csv(pts, csv_path)
    pages = sorted({p['page'] for p in pts})
    print(f'I/O 页 {len(pages)} 页, 点位 {len(pts)} 个, 有线号 {sum(1 for p in pts if p["wire"])} 个')


if __name__ == '__main__':
    main()
