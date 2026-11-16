"""读取 Sysmac Studio「选择并复制多个项目」放进剪贴板的程序段，导出为 ST 文本。

用法:
    python read_sysmac_clipboard.py [输出目录]
默认输出目录: ./sysmac_export/<时间戳>/

输出:
    <程序段名>.st.txt   每个程序段一份 ST 源码（UTF-8）
    variables.txt       剪贴板中附带的变量声明（原始 SLWD 文本）
    clipboard_raw.bin   剪贴板原始数据，备查
    summary.txt         各程序段 ST 块数 / 赋值行数统计
"""
import ctypes
import datetime
import json
import os
import re
import sys
from ctypes import wintypes as w

FORMAT_NAME = 'Omron.SolutionMultipleRootEntites'


def read_clipboard_format(name):
    u = ctypes.WinDLL('user32', use_last_error=True)
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    u.RegisterClipboardFormatW.restype = w.UINT
    u.RegisterClipboardFormatW.argtypes = [w.LPCWSTR]
    u.GetClipboardData.restype = w.HANDLE
    u.GetClipboardData.argtypes = [w.UINT]
    u.OpenClipboard.argtypes = [w.HWND]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [w.HANDLE]
    k.GlobalSize.restype = ctypes.c_size_t
    k.GlobalSize.argtypes = [w.HANDLE]
    k.GlobalUnlock.argtypes = [w.HANDLE]

    fmt = u.RegisterClipboardFormatW(name)
    if not u.OpenClipboard(None):
        sys.exit('无法打开剪贴板（可能被其他程序占用），请重试。')
    try:
        h = u.GetClipboardData(fmt)
        if not h:
            return None
        size = k.GlobalSize(h)
        p = k.GlobalLock(h)
        try:
            return ctypes.string_at(p, size)
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def extract(data):
    """返回 ({程序段名: [ST文本块]}, [变量声明文本])"""
    names = [(m.start(), m.group(1).decode('utf-8', 'replace'))
             for m in re.finditer(rb'ProgramBlockName="([^"]+)"', data)]

    def section_of(pos):
        # 程序段名出现在该段 ST 内容之后
        for npos, name in names:
            if npos > pos:
                return re.sub(r'_0$', '', name)
        return '_未归属'

    dec = json.JSONDecoder()
    sections = {}
    for m in re.finditer(rb'\{"CLs"', data):
        s = data[m.start():m.start() + 4_000_000].decode('utf-8', 'replace')
        try:
            obj, _ = dec.raw_decode(s)
        except ValueError:
            continue
        for cl in obj.get('CLs', []):
            t = cl.get('TXT')
            if t is not None:
                sections.setdefault(section_of(m.start()), []).append(t)

    variables = []
    for m in re.finditer(rb'\[SLWD version=[^\]]*\]', data):
        end = m.start()
        # 取到下一个不可打印的二进制区域为止
        chunk = data[end:end + 4_000_000]
        stop = re.search(rb'\x00{4,}', chunk)
        variables.append(chunk[:stop.start() if stop else len(chunk)]
                         .decode('utf-8', 'replace'))
    return sections, variables


def main():
    data = read_clipboard_format(FORMAT_NAME)
    if not data:
        sys.exit('剪贴板里没有 Sysmac 程序数据。\n'
                 '请在 Sysmac Studio 左侧 POUs 中选中程序段，右键 →「选择并复制多个项目」后再运行。')

    outdir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        'sysmac_export', datetime.datetime.now().strftime('%Y%m%d_%H%M%S'))
    os.makedirs(outdir, exist_ok=True)
    # 先清掉上次导出的程序段，否则用户这次在 Sysmac 删掉的段仍会留在目录里，
    # 后续对点把它当成有效旧程序读进去（「明明删了却还在」）。
    # 只删本脚本自己生成的文件，不动用户放的其他文件。
    for fn in os.listdir(outdir):
        if fn.endswith('.st.txt') or fn in ('variables.txt', 'summary.txt', 'clipboard_raw.bin'):
            try:
                os.remove(os.path.join(outdir, fn))
            except OSError:
                pass
    open(os.path.join(outdir, 'clipboard_raw.bin'), 'wb').write(data)

    sections, variables = extract(data)
    lines = []
    for sec, texts in sections.items():
        # 每个 ST 块（梯形图里的一个内嵌 ST）前加分隔标记，粘贴回去时按块对应
        body = '\r\n'.join(f'//====== ST块 {i} ======\r\n{t}' for i, t in enumerate(texts, 1))
        fn = re.sub(r'[\\/:*?"<>|]', '_', sec) + '.st.txt'
        with open(os.path.join(outdir, fn), 'w', encoding='utf-8', newline='') as f:   # 保持原样 \r\n，避免变成 \r\r\n
            f.write(body)
        lines.append(f'{sec}\t{len(texts)} 个ST块\t{body.count(":=")} 条赋值\t{fn}')
    with open(os.path.join(outdir, 'variables.txt'), 'w', encoding='utf-8') as f:
        f.write('\n\n'.join(variables))
    summary = '\n'.join(lines) or '未找到 ST 程序内容（复制的可能是梯形图或其他对象）'
    with open(os.path.join(outdir, 'summary.txt'), 'w', encoding='utf-8') as f:
        f.write(summary)

    print(f'输出目录: {os.path.abspath(outdir)}')
    print(f'程序段数: {len(sections)}')
    print(summary)


if __name__ == '__main__':
    main()
