"""图纸点已有同地址旧变量但功能不匹配时，也要去程序里找相似候选。

用户反馈：A面涂布站驱动侧 同地址旧变量是「基准辊压辊按钮」（不搭），
但程序里明明有「ID_机头A_驱动侧急停」(0.80)。旧逻辑第314行因 r['var'] 非空
直接跳过，从不搜索候选，导致网页上什么建议都没有。
"""
from pathlib import Path
import sys
import unittest

IO = Path(__file__).resolve().parents[1] / 'scripts' / 'sysmac-io-check'
sys.path.insert(0, str(IO))
from adaptive_workflow import compare
from io_address import parse_address, parse_program
from review_actions import point_task
import tempfile


class SuggestionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old = Path(self.temp.name) / '旧程序'
        old.mkdir()
        # 同地址一个不搭的变量 + 程序里真正相似的另一个变量
        (old / 'test.st.txt').write_text(
            '//====== ST块 1 ======\n'
            'ID_基准辊压辊按钮:=A_I1_00;\n'
            'ID_机头A_驱动侧急停:=A_I2_00;\n', encoding='utf-8')
        self.program = parse_program(old)[0]
        a1, a2 = parse_address('A_I1_00'), parse_address('A_I2_00')
        self.points = [
            {'address': a1.to_dict(), 'wire': a1.program_token(), 'original_wire': 'A_I1_00',
             'function': 'A面涂布站驱动侧', 'recognized': True, 'page': 1, 'title': 'I/O点接线图', 'void': ''},
        ]

    def test_suggestion_given_even_when_same_address_var_exists(self):
        results = compare({'points': self.points}, self.program, {})[0]
        r = results[0]
        self.assertTrue(r.get('suggestions'))
        self.assertTrue(any(s['var'] == 'ID_机头A_驱动侧急停' for s in r['suggestions']))

    def test_suggestion_reaches_task_sources(self):
        results = compare({'points': self.points}, self.program, {})[0]
        task = point_task(results[0], self.program, 'rev', 1, {}, {'test'})
        variables = [s['var'] for s in task['sources']]
        self.assertIn('ID_机头A_驱动侧急停', variables)

    def test_strong_marked_for_same_station_high_score(self):
        """同机构、高分、编号一致、无方向冲突的候选标 strong，供网页优先展示"""
        results = compare({'points': self.points}, self.program, {})[0]
        sug = [s for s in results[0].get('suggestions', [])
               if s['var'] == 'ID_机头A_驱动侧急停']
        self.assertTrue(sug)
        self.assertTrue(sug[0].get('strong'))

    def test_existing_mapping_not_auto_moved(self):
        """即使有更相似候选，也不自动挪走同地址既有变量——只交给用户判断"""
        results, _, decisions, _ = compare({'points': self.points}, self.program, {})
        self.assertEqual(results[0]['status'], '❓ 待判断')
        self.assertFalse(decisions)


if __name__ == '__main__':
    unittest.main()
