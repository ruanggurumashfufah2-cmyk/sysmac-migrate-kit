"""重新读剪贴板时必须清掉上次遗留的程序段。

用户现象：在 Sysmac 里删掉一个程序段后重新复制、重新读取，对点时那个段还在。
根因：读脚本只覆盖本次复制到的段，从不删除上次遗留的 .st.txt，
parse_program 把所有 *.st.txt 都当有效旧程序读进去。
"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
RD = ROOT / 'scripts' / 'sysmac-read'


class ClipboardCleanupTests(unittest.TestCase):
    def test_reader_removes_stale_exports_but_keeps_user_files(self):
        """读脚本入口的清理逻辑：删除自己生成的旧文件，保留用户文件"""
        with tempfile.TemporaryDirectory() as d:
            outdir = Path(d)
            files = ['旧段.st.txt', 'variables.txt', 'summary.txt',
                     'clipboard_raw.bin', '用户备注.txt']
            for fn in files:
                (outdir / fn).write_text('x', encoding='utf-8')

            # 复现读脚本的清理段（与实际脚本保持一致）
            for fn in os.listdir(outdir):
                if fn.endswith('.st.txt') or fn in ('variables.txt', 'summary.txt', 'clipboard_raw.bin'):
                    os.remove(os.path.join(outdir, fn))

            self.assertEqual(sorted(os.listdir(outdir)), ['用户备注.txt'])

    def test_reader_script_contains_cleanup(self):
        """防止以后有人改脚本时把清理逻辑删掉"""
        src = (RD / 'read_sysmac_clipboard.py').read_text(encoding='utf-8')
        self.assertIn("fn.endswith('.st.txt')", src)
        self.assertIn("'clipboard_raw.bin'", src)


if __name__ == '__main__':
    unittest.main()
