from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

IO=Path(__file__).resolve().parents[1]/'scripts/sysmac-io-check'
sys.path.insert(0,str(IO))
from adaptive_workflow import compare,run
from io_address import parse_address,parse_program
from review_actions import point_identity
from openpyxl import load_workbook


class CrossMatchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.old=self.root/'旧程序';self.old.mkdir()
        self.pdf=self.root/'图纸.pdf';self.pdf.write_bytes(b'fixture');self.out=self.root/'输出'

    def source(self,lines):
        (self.old/'test.st.txt').write_text('//====== ST块 1 ======\n'+'\n'.join(lines)+'\n',encoding='utf-8')
        return parse_program(self.old)[0]

    def point(self,wire,function,page=1):
        a=parse_address(wire)
        return {'address':a.to_dict(),'wire':a.program_token(),'original_wire':wire,'function':function,
                'recognized':True,'page':page,'title':'DI模块总览图','void':''}

    def pictured(self):
        program=self.source(['DI_Uw_DS_顶筒伸出按钮:=NOT C3_I1_13;',
                             'DI_Uw_DS_顶筒收回按钮:=C3_I1_14;',
                             'DI_Uw_OS_电机前进按钮:=NOT C3_I2_1;',
                             'DI_Uw_OS_电机后退按钮:=C3_I2_2;'])
        points=[self.point('C3_I1_13','操作侧夹头电机前进按钮'),self.point('C3_I1_14','操作侧夹头电机后退按钮'),
                self.point('C3_I2_01','驱动侧顶筒伸出按钮',2),self.point('C3_I2_02','驱动侧顶筒缩回按钮',2)]
        return program,points

    def check(self,program,points,feedback=None,reviews=None):return compare({'points':points},program,feedback or {},reviews)

    def test_pictured_two_pairs_resolve_side_motion_synonym_and_padding(self):
        program,points=self.pictured();rows,questions,decisions,extra=self.check(program,points)
        self.assertFalse(questions);self.assertFalse(extra);self.assertEqual(len(decisions),4)
        self.assertEqual([r['var'] for r in rows],[program[i]['var'] for i in (2,3,0,1)])
        self.assertTrue(all(r['status']=='⚠️ 成组错位，标黄执行' for r in rows))
        self.assertEqual(decisions[('test',2)]['program_token'],'C3_I2_1')
        self.assertEqual(decisions[('test',4)]['program_token'],'C3_I1_13')
        self.assertTrue(all(r['automatic_swap']['count']==2 for r in rows))

    def test_end_to_end_preserves_expressions_reports_basis_and_has_zero_audit(self):
        _,points=self.pictured()
        data={'pdf_sha256':'fixture','points':points,'references':{},'unrecognized_pages':[]}
        original=(self.old/'test.st.txt').read_bytes()
        with patch('adaptive_workflow.extract',return_value=data),redirect_stdout(io.StringIO()):status=run(self.pdf,self.old,self.out)
        self.assertEqual(status['questions'],0);self.assertEqual(status['audit'],0)
        self.assertEqual(status['changes'],4);self.assertEqual(status['yellow'],4)
        generated=(self.out/'新程序段/test.st.txt').read_text(encoding='utf-8')
        self.assertIn('DI_Uw_DS_顶筒伸出按钮:=NOT C3_I2_1;',generated)
        self.assertIn('DI_Uw_DS_顶筒收回按钮:=C3_I2_2;',generated)
        self.assertIn('DI_Uw_OS_电机前进按钮:=NOT C3_I1_13;',generated)
        self.assertEqual((self.old/'test.st.txt').read_bytes(),original)
        w=load_workbook(self.out/'对点报告.xlsx',read_only=True,data_only=True)
        rows=list(w['对点结果'].values);m=rows[0].index('处理数据')
        self.assertTrue(all(json.loads(r[m])['automatic_swap']['source_wire'] for r in rows[1:]));w.close()

    def test_output_cycle_keeps_full_and_or_interlock(self):
        program=self.source(['C3_Q1_00:=DQ_Uw_风机启动 AND SafetyOK OR Test[1];','C3_Q1_01:=DQ_Uw_风机停止 AND NOT Trip;'])
        points=[self.point('C3_Q1_00','风机停止'),self.point('C3_Q1_01','风机启动')]
        data={'pdf_sha256':'fixture','points':points,'references':{},'unrecognized_pages':[]}
        with patch('adaptive_workflow.extract',return_value=data),redirect_stdout(io.StringIO()):status=run(self.pdf,self.old,self.out)
        self.assertEqual(status['audit'],0);self.assertEqual(status['changes'],2)
        text=(self.out/'新程序段/test.st.txt').read_text(encoding='utf-8')
        self.assertIn('C3_Q1_01:=DQ_Uw_风机启动 AND SafetyOK OR Test[1];',text)
        self.assertIn('C3_Q1_00:=DQ_Uw_风机停止 AND NOT Trip;',text)

    def test_three_way_cycle_is_atomic_and_order_independent(self):
        program=self.source(['DI_压力1:=C3_I1_00;','DI_压力2:=C3_I1_01;','DI_压力3:=C3_I1_02;'])
        points=[self.point('C3_I1_00','压力2'),self.point('C3_I1_01','压力3'),self.point('C3_I1_02','压力1')]
        for ordered in (points,points[::-1]):
            rows,questions,decisions,_=self.check(program,ordered)
            self.assertFalse(questions);self.assertEqual(len(decisions),3)
            self.assertTrue(all(r['automatic_swap']['count']==3 for r in rows))

    def test_explicit_pending_locks_whole_pair_but_other_pair_can_resolve(self):
        program,points=self.pictured()
        feedback={f'{points[0]["original_wire"]}|{points[0]["function"]}|{program[0]["var"]}':{'judge':'待判断'}}
        rows,questions,_,_=self.check(program,points,feedback)
        self.assertEqual(len(questions),2)
        self.assertFalse(rows[0].get('automatic_swap'));self.assertFalse(rows[2].get('automatic_swap'))
        self.assertTrue(rows[1].get('automatic_swap'));self.assertTrue(rows[3].get('automatic_swap'))

    def test_saved_confirm_defer_ignore_or_invalid_action_never_gets_overridden(self):
        program,points=self.pictured()
        for action in ('confirm','defer','ignore','new'):
            reviews={tuple(point_identity(points[0])):{'action':action,'values':{}}}
            rows,_,_,_=self.check(program,points,reviews=reviews)
            self.assertFalse(rows[0].get('automatic_swap'),action)
            self.assertFalse(rows[2].get('automatic_swap'),action)

    def test_tied_source_blocks_whole_pair(self):
        program,points=self.pictured()
        duplicate={**program[2],'line':10,'address':parse_address('C3_I3_0').to_dict()}
        program.append(duplicate)
        rows,questions,_,_=self.check(program,points)
        self.assertEqual(len(questions),2)
        self.assertFalse(rows[0].get('automatic_swap'));self.assertFalse(rows[2].get('automatic_swap'))

    def test_open_chain_duplicate_drawing_or_duplicate_old_address_is_not_a_cycle(self):
        program,points=self.pictured()
        cases=[(program,points[:2]),(program,points+[dict(points[0],page=20)]),
               (program+[dict(program[2],line=20)],points)]
        for ps,ds in cases:
            rows,_,_,_=self.check(ps,ds)
            self.assertFalse(rows[0].get('automatic_swap'))

    def test_different_station_or_signal_type_does_not_form_cycle(self):
        program=self.source(['DI_风机启动:=C3_I1_00;','DI_风机停止:=D3_I1_00;',
                             'C3_Q1_00:=DQ_风机停止;'])
        points=[self.point('C3_I1_00','风机停止'),self.point('D3_I1_00','风机启动'),self.point('C3_Q1_00','风机启动')]
        rows,_,_,_=self.check(program,points)
        self.assertFalse(any(r.get('automatic_swap') for r in rows))

    def test_numbers_axis_and_button_lamp_identity_are_not_erased(self):
        program=self.source(['DI_UwA_顶筒伸出按钮:=C3_I1_00;','DI_UwB_顶筒缩回按钮:=C3_I1_01;'])
        points=[self.point('C3_I1_00','B轴顶筒伸出按钮'),self.point('C3_I1_01','A轴顶筒缩回按钮')]
        rows,_,_,_=self.check(program,points)
        self.assertFalse(any(r.get('automatic_swap') for r in rows))

        program=self.source(['DI_压力1:=C3_I1_00;','DI_压力2:=C3_I1_01;'])
        points=[self.point('C3_I1_00','压力2'),self.point('C3_I1_01','压力3')]
        rows,_,_,_=self.check(program,points)
        self.assertFalse(any(r.get('automatic_swap') for r in rows))
        program=self.source(['DI_电机前进按钮:=C3_I1_00;','DI_电机后退按钮:=C3_I1_01;'])
        points=[self.point('C3_I1_00','电机后退按钮灯'),self.point('C3_I1_01','电机前进按钮灯')]
        rows,_,_,_=self.check(program,points)
        self.assertFalse(any(r.get('automatic_swap') for r in rows))

    def test_near_ties_or_insufficient_improvement_do_not_apply(self):
        program=self.source(['DI_压力1:=C3_I1_00;','DI_压力2:=C3_I1_01;'])
        points=[self.point('C3_I1_00','压力2'),self.point('C3_I1_01','压力1')]
        def scoring(gap):
            return lambda function,p,*_: .92 if function==p['var'][3:] else .92-gap
        for gap in (.0,.05,.1199):
            with patch('cross_match.cross_score',side_effect=scoring(gap)):
                rows,_,_,_=self.check(program,points)
            self.assertFalse(any(r.get('automatic_swap') for r in rows))
        with patch('cross_match.cross_score',side_effect=scoring(.12)):
            rows,_,_,_=self.check(program,points)
        self.assertTrue(all(r.get('automatic_swap') for r in rows))


if __name__=='__main__':unittest.main()
