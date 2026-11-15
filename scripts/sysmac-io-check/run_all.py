"""一键：图纸 PDF + 旧程序段 → 对点报告 + 新程序段

用法:
    python run_all.py <图纸.pdf> <旧程序段目录> <输出目录>

输出目录里:
    图纸点表.csv       图纸 I/O 点表（线号已是 ID_A01_00 写法）
    对点报告.xlsx      对点结果 / IO映射粘贴 / 程序有图纸无 / 同义词 / 统计
    新程序段/          每段 <段名>.st.txt + 新增变量.txt + 变更清单.xlsx + 待判断.xlsx
人工判断：在 对点报告.xlsx 填「你的判断」后，运行
    python learn_feedback.py <输出目录>/对点报告.xlsx
再重新运行本脚本即可（判断存在 <输出目录>/对点反馈.json，自动沿用）。
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run(script, *args):
    print(f'\n>>> {script}', flush=True)
    r = subprocess.run([sys.executable, '-W', 'ignore', os.path.join(HERE, script), *args],
                       env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    if r.returncode:
        sys.exit(f'{script} 失败（返回 {r.returncode}）。如提示 PermissionError，先关闭 Excel 里打开的同名文件。')


def main():
    if len(sys.argv) not in (4,5) or (len(sys.argv)==5 and sys.argv[4]!='--reuse'):
        sys.exit(__doc__)
    pdf, prog, out = sys.argv[1:4]
    reuse = '--reuse' in sys.argv
    from io_address import parse_program, load_rules
    from pathlib import Path
    rules=Path(prog).parent/'命名规则.json'
    load_rules(rules if rules.exists() else None)
    rows, _ = parse_program(prog)
    from adaptive_drawing import needs_adaptive
    if any(p['address']['scheme']!='legacy' for p in rows) or needs_adaptive(pdf):
        from adaptive_workflow import run as adaptive_run
        adaptive_run(pdf,prog,out,reuse)
        return
    os.makedirs(out, exist_ok=True)
    csv_path = os.path.join(out, '图纸点表.csv')
    report = os.path.join(out, '对点报告.xlsx')
    if not reuse or not os.path.exists(csv_path):
        run('extract_drawing_io.py', pdf, csv_path)
    run('compare_io.py', csv_path, prog, report)
    run('generate_program.py', csv_path, prog, os.path.join(out, '新程序段'),
        os.path.join(out, '对点反馈.json'))
    run('audit_program.py', csv_path, os.path.join(out, '新程序段'))
    print(f'\n完成 → {os.path.abspath(out)}')


if __name__ == '__main__':
    main()
