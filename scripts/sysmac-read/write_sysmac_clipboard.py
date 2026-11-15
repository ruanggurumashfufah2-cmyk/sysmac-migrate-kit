"""把改好的程序段写回 Sysmac Studio 剪贴板格式，在 Sysmac 里直接 Ctrl+V 粘贴。

用法:
    python write_sysmac_clipboard.py <原剪贴板 clipboard_raw.bin> <新程序段目录> [--types 新增变量.txt] [--no-clip]

原理（逆向自 Sysmac 复制出来的数据 Omron.SolutionMultipleRootEntites）：
  - 数据是 16 字节 GUID + .NET BinaryFormatter 流，没有整体长度/校验；
    每个实体（EntityRecord）的内容是一个 byte[]（记录类型 0x0F，原始类型 0x02），
    实体里紧跟 byte[] 引用（0x09 + id）的 int32 是 EncodedDataLength。
    InstanceHash 是 .NET 对象身份哈希（<2^26），不是内容校验。
  - 每个内嵌 ST 块的文字存两份：
      PouBody 实体的 JSON：{"CLs":[{"__type":"IST",...,"TXT":"..."}]}，'/' 写成 '\\/'，换行写成 \\u000d\\u000a
      Inline/StructuredText 实体的 XML：<data><StructuredText><Text>...</Text>
    两份都改，并保持一致。
  - 程序的变量声明在 [SLWD] 文本实体里；新程序段引用到、但未声明的外部变量补进 VAR_EXTERNAL 组。
自检：转义规则先对原内容做往返验证（必须逐字节一致）；写好后重新解析，内容必须与新程序段一致。
"""
import ctypes
import json
import os
import re
import struct
import sys
from ctypes import wintypes as w

FORMAT_NAME = 'Omron.SolutionMultipleRootEntites'
MARK = re.compile(r'//====== ST块 (\d+) ======\r?\n')


# ---------------- 转义 ----------------
def json_escape(s):
    out = []
    for ch in s:
        o = ord(ch)
        if ch == '\\':
            out.append('\\\\')
        elif ch == '"':
            out.append('\\"')
        elif ch == '/':
            out.append('\\/')
        elif o < 0x20 or o == 0x7f:
            out.append('\\u%04x' % o)
        else:
            out.append(ch)
    return ''.join(out)


def xml_escape(s, escape_gt=False):
    s = s.replace('&', '&amp;').replace('<', '&lt;')
    return s.replace('>', '&gt;') if escape_gt else s


def xml_unescape(s):
    return (s.replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
             .replace('&apos;', "'").replace('&#xD;', '\r').replace('&#xA;', '\n').replace('&amp;', '&'))


# ---------------- 结构 ----------------
def find_arrays(d):
    """返回 [{oid, rec_start, data_start, cap, used, len_pos}]：被实体引用的 byte[]"""
    arrs = []
    for m in re.finditer(rb'\x0f(....)(....)\x02', d, re.S):
        oid, cap = struct.unpack('<ii', m.group(1) + m.group(2))
        if not (0 < oid < 10_000_000 and 0 < cap <= len(d) - m.end()):
            continue
        ref = d.find(b'\x09' + struct.pack('<i', oid))
        if ref < 0 or ref > m.start():
            continue
        used = struct.unpack('<i', d[ref + 5:ref + 9])[0]
        if not (0 <= used <= cap):
            continue
        arrs.append({'oid': oid, 'rec': m.start(), 'data': m.end(), 'cap': cap,
                     'used': used, 'len_pos': ref + 5})
    return arrs


def ist_spans(text):
    """PouBody JSON 文本里每个 IST 的 TXT 原文位置（按出现顺序）"""
    spans = []
    for m in re.finditer(r'"__type":"IST"', text):
        t = text.find('"TXT":"', m.end())
        if t < 0:
            continue
        a = t + len('"TXT":"')
        b = a
        while True:
            b = text.find('"', b)
            # 前面连续反斜杠为偶数才是真正的结束引号
            k = b - 1
            n = 0
            while text[k] == '\\':
                n += 1
                k -= 1
            if n % 2 == 0:
                break
            b += 1
        spans.append((a, b))
    return spans


def read_blocks(path):
    t = open(path, encoding='utf-8', newline='').read()
    parts = MARK.split(t)
    return {int(parts[i]): parts[i + 1] for i in range(1, len(parts), 2)}


def norm(s):
    return s.replace('\r\n', '\n').rstrip('\n')


def main():
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    raw_path, new_dir = os.path.abspath(args[0]), os.path.abspath(args[1])
    old_dir = os.path.dirname(os.path.abspath(raw_path))
    types_path = None
    if '--types' in sys.argv:
        types_path = sys.argv[sys.argv.index('--types') + 1]
    no_clip = '--no-clip' in sys.argv
    manifest_path = os.path.join(new_dir, '迁移状态.json')
    if not no_clip and os.path.exists(manifest_path):
        with open(manifest_path, encoding='utf-8') as f:
            manifest = json.load(f)
        if manifest.get('ready') is not True:
            sys.exit('当前输出是核对稿，尚有未完成的迁移判断，不能写入 Sysmac 剪贴板。')
        import hashlib
        def digest(path):
            h=hashlib.sha256()
            with open(path,'rb') as f:
                for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
            return h.hexdigest()
        for folder, field in ((old_dir,'source_hashes'),(new_dir,'output_hashes')):
            expected=manifest.get(field,{})
            if not expected:
                sys.exit('迁移验证信息不完整，请重新生成。')
            current_st={fn for fn in os.listdir(folder) if fn.endswith('.st.txt')}
            if current_st!={fn for fn in expected if fn.endswith('.st.txt')}:
                sys.exit('程序段列表已变化，请重新对点并生成。')
            for filename,sha in expected.items():
                path=os.path.join(folder,filename)
                if os.path.basename(filename)!=filename or not os.path.isfile(path) or digest(path)!=sha:
                    sys.exit('程序或变量文件已变化，请重新对点并生成。')
        feedback_path=os.path.join(os.path.dirname(new_dir),'对点反馈.json')
        feedback_sha=digest(feedback_path) if os.path.exists(feedback_path) else None
        if feedback_sha!=manifest.get('feedback_sha256'):
            sys.exit('判断已变化，请重新生成后再写入。')
        rules_path=manifest.get('rules_path','')
        rules_sha=digest(rules_path) if os.path.isfile(rules_path) else None
        if rules_sha!=manifest.get('rules_sha256'):
            sys.exit('命名规则已变化，请重新提取并生成。')
        if not os.path.isfile(manifest.get('pdf','')) or digest(manifest['pdf'])!=manifest.get('pdf_sha256'):
            sys.exit('图纸已变化，请重新提取并生成。')

    d = open(raw_path, 'rb').read()
    arrs = find_arrays(d)
    names = [(m.start(), m.group(1).decode('utf-8')) for m in re.finditer(rb'ProgramBlockName="([^"]+)"', d)]

    def section_of(pos):
        for npos, name in names:
            if npos > pos:
                return re.sub(r'_0$', '', name)
        return None

    BOM = b'\xef\xbb\xbf'
    edits = {}            # oid -> new bytes
    pou = [a for a in arrs if d[a['data']:a['data'] + 10].startswith(BOM + b'{"CLs"')]
    inl = [a for a in arrs if d[a['data']:a['data'] + 80].startswith(BOM + b'<?xml') and
           b'<data><StructuredText><Text>' in d[a['data']:a['data'] + 120]]
    slwd = [a for a in arrs if d[a['data']:a['data'] + 20].startswith(b'[SLWD')]

    # Inline XML：文本 → 实体列表
    xml_by_text = {}
    for a in inl:
        x = d[a['data']:a['data'] + a['used']].decode('utf-8')
        m = re.search(r'<Text>(.*)</Text>', x, re.S)
        txt = xml_unescape(m.group(1))
        escape_gt = xml_escape(txt) != m.group(1)
        assert xml_escape(txt, escape_gt) == m.group(1), f'XML 转义往返不一致（实体 {a["oid"]}），停止'
        xml_by_text.setdefault(txt, []).append((a, x, m, escape_gt))

    # 逐个 PouBody：按段、按块替换
    counter = {}
    changed_blocks, sec_vars = [], {}
    for a in pou:
        sec = section_of(a['rec'])
        if sec is None:
            continue
        fn = os.path.join(new_dir, sec + '.st.txt')
        old_fn = os.path.join(old_dir, sec + '.st.txt')
        if not os.path.exists(fn):
            continue
        new_blocks, old_blocks = read_blocks(fn), read_blocks(old_fn)
        text = d[a['data'] + 3:a['data'] + a['used']].decode('utf-8')
        out, last = [], 0
        for a_, b_ in ist_spans(text):
            counter[sec] = counter.get(sec, 0) + 1
            k = counter[sec]
            esc = text[a_:b_]
            old_txt = json.loads('"' + esc + '"')
            assert json_escape(old_txt) == esc, f'JSON 转义往返不一致（{sec} 块{k}），停止'
            assert norm(old_txt) == norm(old_blocks.get(k, '')), f'{sec} 块{k} 与导出的旧程序不一致，停止'
            nb = new_blocks.get(k)
            if nb is None or norm(nb) == norm(old_txt):
                continue
            tail = old_txt[len(old_txt.rstrip('\r\n')):]          # 保留原来块末的空行
            new_txt = nb.replace('\r\n', '\n').rstrip('\n').replace('\n', '\r\n') + tail
            out.append(text[last:a_] + json_escape(new_txt))
            last = b_
            changed_blocks.append((sec, k))
            sec_vars.setdefault(sec, set()).update(re.findall(r'[A-Za-z_一-鿿][\w一-鿿]*', re.sub(r'//.*', '', new_txt)))
            # 对应的 Inline XML
            cands = xml_by_text.get(old_txt) or []
            assert cands, f'{sec} 块{k} 找不到对应的 Inline ST 源码，停止'
            assert len(cands) == 1, f'{sec} 块{k} 的原文和别的块完全相同，无法确定对应的源码实体，停止'
            xa, x, m, escape_gt = cands.pop(0)
            edits[xa['oid']] = (BOM + (x[1:m.start(1)] + xml_escape(new_txt, escape_gt) + x[m.end(1):]).encode('utf-8')) \
                if x.startswith('﻿') else (x[:m.start(1)] + xml_escape(new_txt, escape_gt) + x[m.end(1):]).encode('utf-8')
        if out:
            out.append(text[last:])
            edits[a['oid']] = BOM + ''.join(out).encode('utf-8')

    # 变量声明：补 VAR_EXTERNAL
    types = {}
    if types_path and os.path.exists(types_path):
        for line in open(types_path, encoding='utf-8-sig').read().splitlines():
            f = line.split('\t')
            if len(f) >= 2:
                types[f[0]] = f[1]
    added_ext = []
    slwd_sorted = sorted(slwd, key=lambda a: a['rec'])
    pou_prog = {}
    for a in pou:
        owner = [s for s in slwd_sorted if s['rec'] < a['rec']]
        if owner:
            pou_prog.setdefault(owner[-1]['oid'], set()).add(section_of(a['rec']))
    for s in slwd_sorted:
        txt = d[s['data']:s['data'] + s['used']].decode('utf-8')
        declared = set(re.findall(r'N=([^\t\r\n]+)', txt))
        need = set()
        for sec in pou_prog.get(s['oid'], ()):
            need |= sec_vars.get(sec, set())
        add = []
        for v in sorted(need - declared):
            if re.fullmatch(r'(ID|OD)_[A-Z]\w+', v):
                add.append((v, 'BOOL'))
            elif re.fullmatch(r'(AD|DA)_[A-Z][0-9A-Z]{1,3}_\d+', v):
                add.append((v, 'INT'))
            elif v in types:
                add.append((v, types[v]))
        if '空块占位' in need and '空块占位' not in declared:
            gv = re.search(r'\+GN=VAR\t[^\r\n]*\r?\n', txt)
            assert gv, 'SLWD 里没有 VAR 组，停止'
            txt = txt[:gv.end()] + '++D=BOOL\tN=空块占位\tG=VAR\r\n' + txt[gv.end():]
            edits[s['oid']] = txt.encode('utf-8')
            added_ext.append('空块占位(局部)')
        if not add:
            continue
        hdr = re.search(r'\+GN=VAR_EXTERNAL[^\r\n]*\r?\n', txt)
        assert hdr, 'SLWD 里没有 VAR_EXTERNAL 组，停止'
        ins = ''.join(f'++D={t}\tN={v}\tG=VAR_EXTERNAL\r\n' for v, t in add)
        txt = txt[:hdr.end()] + ins + txt[hdr.end():]
        edits[s['oid']] = txt.encode('utf-8')
        added_ext += [v for v, _ in add]

    # 拼接：替换 byte[] 记录（长度+内容）和 EncodedDataLength
    reps = []
    by_oid = {a['oid']: a for a in arrs}
    for oid, data in edits.items():
        a = by_oid[oid]
        reps.append((a['rec'], a['data'] + a['cap'], b'\x0f' + struct.pack('<ii', oid, len(data)) + b'\x02' + data))
        reps.append((a['len_pos'], a['len_pos'] + 4, struct.pack('<i', len(data))))
    reps.sort(key=lambda r: r[0], reverse=True)
    nd = bytearray(d)
    for s_, e_, r in reps:
        nd[s_:e_] = r
    nd = bytes(nd)

    # 自检：重新解析，内容必须与新程序段一致
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import read_sysmac_clipboard as R
    secs, _ = R.extract(nd)
    bad = []
    for sec, texts in secs.items():
        fn = os.path.join(new_dir, sec + '.st.txt')
        if not os.path.exists(fn):
            continue
        nb = read_blocks(fn)
        for k, t in enumerate(texts, 1):
            if norm(t) != norm(nb.get(k, t)):
                bad.append((sec, k))
    for a in find_arrays(nd):
        x = nd[a['data']:a['data'] + a['used']]
        if x.startswith(BOM + b'{"CLs"'):
            for line in x[3:].decode('utf-8').split('\r\n'):
                if line.strip():
                    json.loads(line)
    assert not bad, f'自检失败：{bad[:5]}'

    out_bin = os.path.join(new_dir, 'clipboard_new.bin')
    open(out_bin, 'wb').write(nd)
    print(f'改动 ST 块 {len(changed_blocks)} 个，改写实体 {len(edits)} 个，补外部变量声明 {len(added_ext)} 个')
    print(f'自检通过 → {out_bin}')
    if not no_clip:
        put_clipboard(nd)
        print('已放入剪贴板：在 Sysmac 测试工程的 POUs 里 Ctrl+V 粘贴')


def put_clipboard(data):
    u = ctypes.WinDLL('user32', use_last_error=True)
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    u.RegisterClipboardFormatW.restype = w.UINT
    u.RegisterClipboardFormatW.argtypes = [w.LPCWSTR]
    u.SetClipboardData.restype = w.HANDLE
    u.SetClipboardData.argtypes = [w.UINT, w.HANDLE]
    u.OpenClipboard.argtypes = [w.HWND]
    k.GlobalAlloc.restype = w.HGLOBAL
    k.GlobalAlloc.argtypes = [w.UINT, ctypes.c_size_t]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [w.HGLOBAL]
    k.GlobalUnlock.argtypes = [w.HGLOBAL]
    fmt = u.RegisterClipboardFormatW(FORMAT_NAME)
    h = k.GlobalAlloc(0x0002, len(data))   # GMEM_MOVEABLE
    p = k.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    k.GlobalUnlock(h)
    if not u.OpenClipboard(None):
        sys.exit('无法打开剪贴板，请重试')
    try:
        u.EmptyClipboard()
        if not u.SetClipboardData(fmt, h):
            sys.exit('写剪贴板失败')
    finally:
        u.CloseClipboard()


if __name__ == '__main__':
    main()
