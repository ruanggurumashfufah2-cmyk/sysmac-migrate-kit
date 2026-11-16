"""图纸编号写法与程序数组下标的语义归一。

现场规律：图纸把编号写在功能文字里（一层02#、温控表1ALM、二层10-3#），
程序把编号放在数组下标里（[2]、[1]、[10,4]）。两边不归一，相似度被编号
差异拉低，大批本该配上的点被误判「相似度不足50%」。
同时方向、按钮/按钮灯的身份不能被抹掉（用户原则：不能把按钮当按钮灯）。
"""
from pathlib import Path
import sys
import unittest

IO = Path(__file__).resolve().parents[1] / 'scripts' / 'sysmac-io-check'
sys.path.insert(0, str(IO))
from adaptive_workflow import semantic, similarity


def row(var, kind='DI'):
    return {'var': var, 'address': {'kind': kind}}


class SemanticTests(unittest.TestCase):
    def test_drawing_number_matches_program_index(self):
        cases = [
            ('一层02#循环风机变频ALM', 'ID_A面烘箱循环风机变频器报警[2]'),
            ('二层09#排风风机变频ALM', 'ID_A面烘箱排风机变频器报警[9]'),
            ('一层01-2#主传动电机ALM', 'ID_A面烘箱主传动电机热继报警[1,3]'),
        ]
        for func, var in cases:
            self.assertGreaterEqual(similarity(func, row(var)), 0.5, (func, var))

    def test_alm_and_relay_synonyms(self):
        self.assertGreaterEqual(similarity('二层14#主传动变频器ALM',
                                           row('ALM_Date主传动变频器报警信号[34]')), 0.5)

    def test_button_lamp_not_erased(self):
        """按钮 != 按钮灯，不能因为同义词把「灯」抹掉而误判为同一个点"""
        self.assertNotEqual(semantic('电机按钮'), semantic('电机按钮灯'))

    def test_direction_not_erased(self):
        self.assertNotEqual(semantic('气缸伸出位'), semantic('气缸缩回位'))


if __name__ == '__main__':
    unittest.main()
