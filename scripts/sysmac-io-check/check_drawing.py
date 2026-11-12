"""图纸自查：找出可能画错的地方（不改任何东西，只列疑点）

1. 同一功能出现在多个点（排除同一页相邻通道的合并单元格）
2. A/B 对称工位对不上：机头A(B区)↔机头B(F区)、出料A(C区)↔出料B(G区)，同模块同通道功能应只差 A/B
3. 同一模块号既有数字量线号又有模拟量线号
"""
import re
from collections import defaultdict

ALIAS = [('A面涂布', '机头A'), ('B面涂布', '机头B'), ('A面牵引', '出料A'), ('B面牵引', '出料B'),
         ('机头1#', '机头A'), ('机头2#', '机头B')]
MIRROR = {'B': 'F', 'C': 'G'}


def norm_func(f):
    f = f or ''
    for a, b in ALIAS:
        f = f.replace(a, b)
    return re.sub(r'[\W_]', '', f)


def _swap(f):
    return (f.replace('机头A', '§1').replace('机头B', '机头A').replace('§1', '机头B')
             .replace('出料A', '§2').replace('出料B', '出料A').replace('§2', '出料B'))


def find_doubts(drawing):
    pts = [p for p in drawing if p['wire'] and p['void'] != '整页作废']
    by = {p['wire']: p for p in pts}
    out = []

    # 1. 同一功能多处
    g = defaultdict(list)
    for p in pts:
        n = norm_func(p['function'])
        if n and not p['void'] and not re.search(r'备用|占用|公共端', n):
            g[n].append(p)
    for n, v in g.items():
        if len(v) < 2:
            continue
        mods = {x['wire'].rsplit('-', 1)[0] for x in v}
        chs = sorted(int(x['wire'].rsplit('-', 1)[1]) for x in v)
        if len(mods) == 1 and chs == list(range(chs[0], chs[0] + len(chs))) and len({x['page'] for x in v}) == 1:
            continue      # 同一模块相邻通道：一般是合并单元格（如 推进气缸 伸出/预伸出/收回）
        out.append(('同一功能出现多处', v,
                    '这些点的功能文字相同，可能有一处是旧的或写错；程序生成时变量保持接在原来的点上'))

    # 2. A/B 对称
    for p in pts:
        k, mod, ch = p['wire'].split('-')
        if mod[0] not in MIRROR:
            continue
        q = by.get(f'{k}-{MIRROR[mod[0]]}{mod[1:]}-{ch}')
        if not q:
            continue
        a, b = norm_func(p['function']), norm_func(q['function'])
        if not a or not b or '备用' in a + b:
            continue
        if a == b and re.search(r'机头[AB]|出料[AB]', a):
            out.append(('A/B对称：写成了同一侧', [p, q], f'两边都写“{p["function"]}”，按对称应一侧为A、一侧为B'))
        elif _swap(a) != b and a != b:
            out.append(('A/B对称：功能不同', [p, q], '对称位置功能不一样，可能是配置差异，也可能是图纸写错'))

    # 3. 模块号数字量/模拟量冲突
    kinds = defaultdict(lambda: defaultdict(list))
    for p in pts:
        k, mod, _ = p['wire'].split('-')
        kinds[mod]['数字量' if k in 'IO' else '模拟量'].append(p)
    for mod, kk in kinds.items():
        if len(kk) > 1:
            v = kk['数字量'][:1] + kk['模拟量'][:1]
            out.append(('模块号冲突', v, f'模块 {mod} 既有数字量线号又有模拟量线号，至少一处线号写错'))
    return out
