import importlib.util
from pathlib import Path
import unittest

writer_path = Path(__file__).resolve().parents[1] / 'scripts' / 'sysmac-read' / 'write_sysmac_clipboard.py'
spec = importlib.util.spec_from_file_location('clipboard_writer', writer_path)
writer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(writer)


class ClipboardXmlTest(unittest.TestCase):
    def test_greater_than_encoding_round_trips(self):
        source = 'IF a &gt; 3 AND b &lt; 5 THEN x := a &amp; b; END_IF;'
        text = writer.xml_unescape(source)
        self.assertEqual(writer.xml_escape(text, escape_gt=True), source)

    def test_raw_greater_than_encoding_round_trips(self):
        source = 'IF a > 3 AND b &lt; 5 THEN x := a &amp; b; END_IF;'
        text = writer.xml_unescape(source)
        self.assertEqual(writer.xml_escape(text), source)


if __name__ == '__main__':
    unittest.main()
