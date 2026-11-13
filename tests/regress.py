"""回归测试：把「改完代码有没有弄坏已有结果」变成一条可重复执行的命令。

用法（在本目录的上一级，即工具包根目录执行）:
    python tests/regress.py --list                 列出所有用例
    python tests/regress.py --check                校验全部用例
    python tests/regress.py --check smoke          只校验某个用例
    python tests/regress.py --snapshot smoke       用当前代码重建该用例的期望结果

用例放在 tests/cases/<用例名>/：
    图纸.pdf            可选。有它就跑全流程（含 PDF 提取）
    图纸点表.csv        可选。没有 PDF 时直接用它，跳过提取步骤
    旧程序/*.st.txt     必需。就是 read_sysmac_clipboard.py 导出的那些文件
    旧程序/variables.txt 可选。剪贴板附带的全局变量声明

期望结果存在 tests/golden/<用例名>.txt（纯文本快照，方便 git diff）。
校验时把本次运行的全部输出序列化成同一份文本再逐行比对，所以
.xlsx 的内容、.st.txt 的每一行、.txt 的每个字符改动都会被发现。

注意：快照只比对内容，不比对单元格颜色/列宽等样式。
"""
import argparse
import difflib
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
IO = os.path.join(ROOT, 'scripts', 'sysmac-io-check')
CASES = os.path.join(HERE, 'cases')
GOLDEN = os.path.join(HERE, 'golden')

# 快照里要包含哪些输出；.html 与 .bin 不进快照（.html 是 .st.txt 的重复展示，.bin 是二进制底稿）
TEXT_EXT = ('.txt', '.csv', '.bat', '.json')
DIFF_LIMIT = 200


def cases():
    if not os.path.isdir(CASES):
        return []
    return sorted(d for d in os.listdir(CASES) if os.path.isdir(os.path.join(CASES, d)))


def run(script, *args):
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8'}
    r = subprocess.run([sys.executable, '-W', 'ignore', os.path.join(IO, script), *args],
                       env=env, capture_output=True, encoding='utf-8', errors='replace')
    if r.returncode:
        tail = '\n'.join(((r.stdout or '') + (r.stderr or '')).strip().splitlines()[-25:])
        raise SystemExit(f'✗ {script} 失败（返回 {r.returncode}）\n{tail}')
    return (r.stdout or '').strip()


def build(case_dir, out):
    """跑一遍流水线，返回各步骤的 stdout 便于排查"""
    prog = os.path.join(case_dir, '旧程序')
    if not os.path.isdir(prog):
        raise SystemExit(f'✗ {case_dir} 里没有 旧程序/ 目录')
    pdf = os.path.join(case_dir, '图纸.pdf')
    csv_in = os.path.join(case_dir, '图纸点表.csv')
    log = []
    if os.path.exists(pdf):
        csv_path = os.path.join(out, '图纸点表.csv')
        log.append(run('extract_drawing_io.py', pdf, csv_path))
    elif os.path.exists(csv_in):
        csv_path = os.path.join(out, '图纸点表.csv')
        shutil.copyfile(csv_in, csv_path)
    else:
        raise SystemExit(f'✗ {case_dir} 里既没有 图纸.pdf 也没有 图纸点表.csv')
    report = os.path.join(out, '对点报告.xlsx')
    log.append(run('compare_io.py', csv_path, prog, report))
    log.append(run('generate_program.py', csv_path, prog, os.path.join(out, '新程序段')))
    log.append(run('audit_program.py', csv_path, os.path.join(out, '新程序段')))
    return log


def norm(text, out, case_dir):
    """把跟机器/临时目录相关的绝对路径换成占位符，快照才能跨机器比对"""
    for path, mark in ((out, '<OUT>'), (case_dir, '<CASE>'), (IO, '<IO>'), (ROOT, '<ROOT>')):
        for p in {os.path.abspath(path), os.path.abspath(path).replace('\\', '/')}:
            text = text.replace(p, mark)
    return text.replace('\r\n', '\n').lstrip('\ufeff')


def read_text(path):
    """先按 UTF-8 读；读不了再按 GBK（.bat 是 GBK，.txt/.csv 是 UTF-8）"""
    raw = open(path, 'rb').read()
    for enc in ('utf-8-sig', 'gbk'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace')


def dump_xlsx(path):
    from openpyxl import load_workbook
    wb = load_workbook(path, data_only=False)
    lines = []
    for ws in wb.worksheets:
        lines.append(f'--- 工作表「{ws.title}」 {ws.max_row} 行 × {ws.max_column} 列 ---')
        for row in ws.iter_rows():
            cells = ['' if c.value is None else str(c.value).strip() for c in row]
            while cells and cells[-1] == '':
                cells.pop()
            if cells:
                lines.append(' | '.join(cells))
    return '\n'.join(lines)


def dump(out, case_dir):
    """把输出目录序列化成一份稳定排序的纯文本"""
    chunks = []
    for dirpath, dirnames, filenames in os.walk(out):
        dirnames.sort()
        for fn in sorted(filenames):
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, out).replace('\\', '/')
            low = fn.lower()
            if low.endswith('.xlsx'):
                body = dump_xlsx(full)
            elif low.endswith(TEXT_EXT):
                body = read_text(full)
            else:
                continue
            chunks.append(f'### {rel}\n{norm(body, out, case_dir).rstrip()}\n')
    return '\n'.join(chunks)


def snapshot(case, quiet=False):
    case_dir = os.path.join(CASES, case)
    tmp = tempfile.mkdtemp(prefix=f'regress_{case}_')
    try:
        out = os.path.join(tmp, '输出')
        os.makedirs(out)
        log = build(case_dir, out)
        text = dump(out, case_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(GOLDEN, exist_ok=True)
    path = os.path.join(GOLDEN, f'{case}.txt')
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(text)
    if not quiet:
        print(f'✓ 已记录 {case} → tests/golden/{case}.txt（{len(text.splitlines())} 行）')
        for line in log:
            if line:
                print('   ' + line.splitlines()[0])
    return path


def check(case):
    golden = os.path.join(GOLDEN, f'{case}.txt')
    if not os.path.exists(golden):
        print(f'✗ {case}: 没有期望结果，先运行 --snapshot {case}')
        return False
    case_dir = os.path.join(CASES, case)
    tmp = tempfile.mkdtemp(prefix=f'regress_{case}_')
    try:
        out = os.path.join(tmp, '输出')
        os.makedirs(out)
        build(case_dir, out)
        text = dump(out, case_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    want = open(golden, encoding='utf-8').read()
    if want == text:
        print(f'✓ {case}: 与期望结果一致')
        return True
    diff = list(difflib.unified_diff(want.splitlines(), text.splitlines(),
                                     fromfile=f'期望 {case}', tofile=f'实际 {case}', lineterm='', n=2))
    print(f'✗ {case}: 输出变了（{sum(1 for d in diff if d[:1] in "+-" and d[:3] not in ("+++", "---"))} 行差异）')
    for line in diff[:DIFF_LIMIT]:
        print('   ' + line)
    if len(diff) > DIFF_LIMIT:
        print(f'   …… 还有 {len(diff) - DIFF_LIMIT} 行差异未显示')
    print('   确认这是你要的改动后，运行 --snapshot 更新期望结果。')
    return False


def main():
    ap = argparse.ArgumentParser(description='Sysmac 迁移工具包回归测试')
    ap.add_argument('--list', action='store_true', help='列出用例')
    ap.add_argument('--check', nargs='*', metavar='用例', help='校验（不给名字则校验全部）')
    ap.add_argument('--snapshot', nargs='+', metavar='用例', help='重建期望结果')
    a = ap.parse_args()
    allc = cases()

    if a.list or not (a.check is not None or a.snapshot):
        print('用例目录: tests/cases/')
        for c in allc:
            has = '有' if os.path.exists(os.path.join(GOLDEN, f'{c}.txt')) else '无'
            files = os.listdir(os.path.join(CASES, c))
            print(f'  {c:20s} 期望结果:{has}   {", ".join(files)}')
        if not allc:
            print('  （还没有用例。把真实项目的 图纸.pdf + 旧程序/ 放进来，见 tests/README.md）')
        return 0

    if a.snapshot:
        for c in a.snapshot:
            if c not in allc:
                raise SystemExit(f'✗ 没有这个用例: {c}')
            snapshot(c)
        return 0

    targets = a.check or allc
    if not targets:
        print('✗ 还没有用例可选')
        return 1
    ok = all(check(c) for c in targets)
    print('\n全部通过' if ok else '\n有用例不通过')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
