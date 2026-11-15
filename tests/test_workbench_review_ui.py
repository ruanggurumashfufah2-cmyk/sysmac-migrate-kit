from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]


class WorkbenchReviewUITests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'),'Node.js is required for the browser logic checks')
    def test_shipped_javascript_batch_workflow(self):
        result=subprocess.run(['node','tests/test_workbench_review.js'],cwd=ROOT,capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
