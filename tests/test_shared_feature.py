"""同线号唯一映射但图纸功能写得很简时的兜底放行。

用户截图：图纸「二层01#烘箱船体升降检测」，程序「ID_B面烘箱船体提升主气缸上升位[1]」
功能名写法差异大但线号已唯一对应。另一类「A面涂布-穿带无刷控制」→压力比例阀
只有工位名重合，属于真疑点，必须仍待判断。
"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

IO = Path(__file__).resolve().parents[1] / 'scripts' / 'sysmac-io-check'
sys.path.insert(0, str(IO))
from adaptive_workflow import compare, _shared_feature
from io_address import parse_address


def point(wire, func):
    a = parse_address(wire)
    return {'address': a.to_dict(), 'wire': a.program_token(), 'original_wire': wire,
            'function': func, 'recognized': True, 'page': 1, 'title': 'I/O点接线图', 'void': ''}


def row(var, wire, line=1):
    a = parse_address(wire)
    return {'address': a.to_dict(), 'var': var, 'simple': True, 'refs': 1,
            'section': 'test', 'line': line, 'raw': f'{var} := {wire};'}


class FeatureTests(unittest.TestCase):
    def test_shared_feature_entity(self):
        self.assertTrue(_shared_feature('二层01#烘箱船体升降检测',
                                        'ID_B面烘箱船体提升主气缸上升位[1]'))

    def test_station_only_is_not_feature(self):
        """纯工位名重合不算功能实体，不能放行"""
        self.assertFalse(_shared_feature('A面涂布-穿带无刷控制',
                                         'REAL_TO_INT(DA_A面涂布站AT9涂布恒压压力)'))

    def test_vague_name_not_released(self):
        self.assertFalse(_shared_feature('信号名称甲', '信号名称乙'))

    def test_same_address_with_feature_is_yellow_not_question(self):
        pts = [point('B_I1_00', '二层01#烘箱船体升降检测')]
        prog = [row('ID_B面烘箱船体提升主气缸上升位[1]', 'B_I1_00')]
        # 真实相似度偏低，用 patch 固定住，验证兜底路径
        with patch('adaptive_workflow.similarity', return_value=0.3):
            results, questions, _, _ = compare({'points': pts}, prog, {})
        self.assertTrue(results[0]['status'].startswith('⚠'))
        self.assertFalse(questions)


if __name__ == '__main__':
    unittest.main()
