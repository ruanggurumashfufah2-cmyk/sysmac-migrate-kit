"""机构代号编址（涂布机图纸/程序）：A面涂布=A、机头从站=I ... 的自动识别。

现场同一张图混用多套规则：
  - 机构代号式：A_I1_00（旧程序）、A-I01-00（图纸）、S-1AI-1+（模拟量）
  - 老式信号式：I-A03-08（I=输入，A03=模块）、A-A06-2+（A=模拟量）
判别的唯一可靠依据是第二段是否为模块标记（I/Q/AI/AQ/带F安全型），
不能用首字符，因为 A/I 既是机构代号又是老式信号字母。
"""
from pathlib import Path
import sys
import unittest

IO = Path(__file__).resolve().parents[1] / 'scripts' / 'sysmac-io-check'
sys.path.insert(0, str(IO))
from io_address import parse_address, find_addresses
import extract_drawing_io as E


class OrgAddressTests(unittest.TestCase):
    def test_org_digital_underscore_and_dash_are_same_key(self):
        """旧程序 B_I1_00 与图纸 B-I01-00 必须归一到同一地址"""
        a, b = parse_address('B_I1_00'), parse_address('B-I01-00')
        self.assertEqual(a.scheme, 'org')
        self.assertEqual(b.scheme, 'org')
        self.assertEqual(a.key, b.key)
        self.assertEqual((a.kind, a.station, a.module, a.channel), ('DI', 'B', '1', 0))

    def test_org_analog_variants(self):
        for token, kind, mod, ch in (
            ('S-1AI-1+', 'AI', '1', 1), ('A_AI1_1', 'AI', '1', 1),
            ('A_2AI_4I+', 'AI', '2', 4), ('A_AQ1_1', 'AO', '1', 1),
            ('A_FI2_00', 'DI', '2', 0), ('A_2FAI_4I+', 'AI', '2', 4),
        ):
            a = parse_address(token)
            self.assertIsNotNone(a, token)
            self.assertEqual((a.scheme, a.kind, a.module, a.channel),
                             ('org', kind, mod, ch), token)

    def test_legacy_signals_are_not_stolen(self):
        """I-A03-08 的 I 是输入、A-A06-2+ 的 A 是模拟量，不能被机构代号规则抢走"""
        for token, kind in (('I-A03-08', 'DI'), ('O-A05-15', 'DO'),
                            ('A-A06-2+', 'AI'), ('D-E54-1+', 'AO')):
            a = parse_address(token)
            self.assertEqual(a.scheme, 'legacy', token)
            self.assertEqual(a.kind, kind, token)

    def test_drawing_extraction_normalizes_org_wire(self):
        """extract_drawing_io 应把图纸 A-I01-00 归一成程序写法 A_I1_00"""
        wire, typo = E.norm_wire_text('A-I01-00')
        self.assertEqual(wire, 'A_I1_00')
        self.assertTrue(typo)
        self.assertEqual(E.norm_wire_text('I-A03-08')[0], 'I-A03-08')

    def test_program_mirror_array_is_found_but_marked(self):
        """触摸屏数组镜像 A_I1[0] := A_I1_00 仍是可解析地址，不丢失源码"""
        hits = list(find_addresses('A_I1[0] := A_I1_00;'))
        self.assertTrue(hits)
        self.assertTrue(any(a.key == 'org:DI:A:1:0' for _, _, a in hits))


if __name__ == '__main__':
    unittest.main()
