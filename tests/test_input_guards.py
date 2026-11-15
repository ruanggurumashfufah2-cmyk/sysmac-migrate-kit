import csv
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
IO = ROOT / 'scripts' / 'sysmac-io-check'


class InputGuards(unittest.TestCase):
    def run_script(self, name, *args):
        return subprocess.run([sys.executable, str(IO / name), *map(str, args)],
                              env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                              capture_output=True, encoding='utf-8')

    def test_empty_points_do_not_overwrite_existing_outputs(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            csv_path = root / 'points.csv'
            csv_path.write_text('页码,图纸原线号,功能,作废\n', encoding='utf-8-sig')
            old = root / 'old'
            old.mkdir()
            report = root / 'report.xlsx'
            report.write_bytes(b'unchanged')
            result = self.run_script('compare_io.py', csv_path, old, report)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('没有有效线号', result.stderr)
            self.assertEqual(report.read_bytes(), b'unchanged')
            for script in ('generate_program.py', 'audit_program.py'):
                result = self.run_script(script, csv_path, old, root / 'output')
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('没有有效线号', result.stderr)
                self.assertFalse((root / 'output').exists())

    def test_unrecognized_program_cannot_generate(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            points = root / 'points.csv'
            with points.open('w', encoding='utf-8-sig', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(['页码', '图纸原线号', '功能', '作废'])
                writer.writerow(['1', 'I-A01-00', '急停', ''])
            old = root / 'old'
            old.mkdir()
            (old / 'test.st.txt').write_text('DI_急停:=C1_I1_0;\n', encoding='utf-8')
            for script in ('compare_io.py', 'generate_program.py'):
                output = root / 'output'
                result = self.run_script(script, points, old, output)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('暂不支持的从站线号', result.stderr)
                self.assertFalse(output.exists())

    def test_mixed_formats_are_rejected_but_comments_are_ignored(self):
        sys.path.insert(0, str(IO))
        import compare_io
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'test.st.txt'
            path.write_text('a:=ID_A01_00; // C1_I1_0\n(*\nb:=Y1_Q2_03;\n*)\n', encoding='utf-8')
            compare_io.check_unsupported_program(d)
            path.write_text('a:=ID_A01_00;\nb:=C1_I1_0;\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'C1_I1_0'):
                compare_io.check_unsupported_program(d)
            path.write_text('a:=ID_A01_00;\nb:=C1_1AI1;\n', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'C1_1AI1'):
                compare_io.check_unsupported_program(d)


if __name__ == '__main__':
    unittest.main()
