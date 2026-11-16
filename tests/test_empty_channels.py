"""图纸 I/O 表里的空槽位（无线号、无功能文字、无地址）不该刷进待判断。

现场图纸每页常有 INPUT8/14/15/16 这类未接线的备用通道。旧流程把它标为
「备用/作废，程序未用」直接跳过；通用识别流程曾把它们全部塞进待判断，
几百条只剩页码（P475）的空洞记录，把真正需要处理的问题淹掉了。
"""
from pathlib import Path
import sys
import unittest

IO = Path(__file__).resolve().parents[1] / 'scripts' / 'sysmac-io-check'
sys.path.insert(0, str(IO))
from adaptive_workflow import compare
from io_address import parse_address, load_rules


def point(page=475, slot='INPUT8', wire='', func='', addr=None):
    return {'address': addr, 'original_wire': wire, 'wire': wire, 'function': func,
            'recognized': False, 'page': page, 'title': '', 'slot': slot,
            'void': '', 'note': '', 'layout': 'legacy-table'}


class EmptyChannelTests(unittest.TestCase):
    def tearDown(self):
        load_rules()

    def test_empty_channels_are_skipped_not_questioned(self):
        data = {'points': [point(475, 'INPUT8'), point(475, 'INPUT16'), point(476, 'INPUT14')]}
        results, questions, decisions, extra = compare(data, [], {})
        self.assertEqual(len(results), 3)
        self.assertTrue(all(r['status'] == '✅ 空通道/备用，程序未用' for r in results))
        self.assertFalse(questions)
        self.assertFalse(decisions)
        self.assertFalse(extra)

    def test_missing_wire_but_function_present_still_needs_review(self):
        """图纸漏写线号但有功能文字，是图纸问题，不能被当成空通道吞掉。"""
        p = point(func='机头A系统急停按钮')
        results, questions, _, _ = compare({'points': [p]}, [], {})
        self.assertEqual(len(questions), 1)
        self.assertIn('格式或功能文字不完整', results[0]['problem_reason'])

    def test_wire_present_but_function_missing_still_needs_review(self):
        a = parse_address('I-A01-00').to_dict()
        p = point(wire='I-A01-00', addr=a)
        _, questions, _, _ = compare({'points': [p]}, [], {})
        self.assertEqual(len(questions), 1)

    def test_real_point_is_not_treated_as_empty_channel(self):
        a = parse_address('I-A01-00').to_dict()
        p = point(wire='I-A01-00', func='机头A系统急停按钮', addr=a)
        p['recognized'] = True
        results, questions, _, _ = compare({'points': [p]}, [], {})
        self.assertNotEqual(results[0]['status'], '✅ 空通道/备用，程序未用')
        self.assertEqual(len(questions), 1)


if __name__ == '__main__':
    unittest.main()
