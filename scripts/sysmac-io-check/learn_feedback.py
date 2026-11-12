"""读取用户在对点报告里填的反馈，保存下来，下次比对自动沿用。

用法: python learn_feedback.py <对点报告.xlsx>

读取:
  对点结果表「你的判断」(对/错/忽略) + 「备注」 → 同目录 对点反馈.json（按 线号|图纸功能|程序变量 记）
  同义词表 → skill 目录 synonyms.json（所有项目共用）
"""
import json
import os
import sys

from openpyxl import load_workbook

SKILL_DIR = os.path.dirname(os.path.abspath(__file__))
SYN_FILE = os.path.join(SKILL_DIR, 'synonyms.json')


def main():
    path = sys.argv[1]
    wb = load_workbook(path, data_only=True)
    ws = wb['对点结果']
    hdr = [c.value for c in ws[1]]
    col = {h: i for i, h in enumerate(hdr)}
    fb_path = os.path.join(os.path.dirname(os.path.abspath(path)), '对点反馈.json')
    fb = json.load(open(fb_path, encoding='utf-8')) if os.path.exists(fb_path) else {}

    added = 0
    remarks = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        judge = (row[col['你的判断']] or '').strip()
        remark = (row[col['备注']] or '').strip()
        if not judge and not remark:
            continue
        wcol = col.get('图纸原线号', col['线号'])
        key = f"{row[wcol] or ''}|{row[col['图纸功能']] or ''}|{row[col['程序变量']] or ''}"
        fb[key] = {'judge': judge, 'remark': remark}
        added += 1
        if remark:
            remarks.append(f"{row[col['线号']]} [{judge}] {row[col['图纸功能']]} ↔ {row[col['程序变量']]}：{remark}")
    json.dump(fb, open(fb_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    syn = []
    if '同义词' in wb.sheetnames:
        for a, b, *_ in wb['同义词'].iter_rows(min_row=2, values_only=True):
            if a:
                syn.append([str(a).strip(), str(b or '').strip()])
        # 长词优先替换，避免短词先把长词拆坏
        seen, uniq = set(), []
        for a, b in syn:
            if a not in seen:
                seen.add(a)
                uniq.append([a, b])
        uniq.sort(key=lambda x: -len(x[0]))
        json.dump(uniq, open(SYN_FILE, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

    print(f'读取判断 {added} 条，累计 {len(fb)} 条 → {fb_path}')
    print(f'同义词 {len(syn)} 条 → {SYN_FILE}')
    if remarks:
        print('\n带备注的行（需要 Claude 看一下）:')
        print('\n'.join(remarks))


if __name__ == '__main__':
    main()
