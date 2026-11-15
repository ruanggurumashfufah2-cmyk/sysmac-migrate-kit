from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

IO=Path(__file__).resolve().parents[1]/'scripts'/'sysmac-io-check'
sys.path.insert(0,str(IO))
from io_address import parse_address, parse_program, code_lines, load_rules, find_addresses
from adaptive_drawing import station_page
from adaptive_workflow import compare
from adaptive_workflow import run
import json
import subprocess


class AddressTests(unittest.TestCase):
    def tearDown(self):load_rules()

    def test_configured_drawing_and_program_naming_are_equivalent(self):
        load_rules(IO/'naming_rules.example.json')
        d=parse_address('S7.DI.03.00');p=parse_address('IN_S7_M3_C00')
        self.assertEqual(d.key,p.key)
        self.assertEqual(d.program_token(),'IN_S7_M3_C00')
        self.assertEqual(len(list(find_addresses('a:=NOT IN_S7_M3_C00;'))),1)
        self.assertFalse(list(find_addresses('a:=IN_S7_M3_C00extra;')))
        self.assertNotEqual(d.key,parse_address('S8.DI.03.00').key)
    def test_padding_and_terminals_have_common_key_but_station_and_type_do_not(self):
        self.assertEqual(parse_address('C1_I1_0').key,parse_address('C1_I01_00').key)
        self.assertEqual(parse_address('C1_1AI1I+').key,parse_address('C1_01AI01-').key)
        self.assertNotEqual(parse_address('C1_I1_0').key,parse_address('D1_I1_0').key)
        self.assertNotEqual(parse_address('C1_I1_0').key,parse_address('C1_Q1_0').key)
        self.assertNotEqual(parse_address('I-A01-00').key,parse_address('C1_I1_0').key)
        self.assertEqual(parse_address('AD-C03-1').key,parse_address('A-C03-1+').key)
        self.assertIsNone(parse_address('C1_I1_00_extra'))

    def test_comment_strings_and_compound_expressions(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'test.st.txt'
            path.write_text("// DI_a:=C1_I1_0;\n(* nested (* c:=X1_I1_00; *) *)\n"
                            "DI_a[1]:=NOT C1_I1_0;\nC1_Q1_0:=DQ_a OR Test[1]; // keep\n"
                            "msg:='C1_I1_02';\n",encoding='utf-8')
            rows,unknown=parse_program(d)
            self.assertEqual(len(rows),2)
            self.assertEqual(rows[0]['var'],'DI_a[1]')
            self.assertEqual(rows[1]['var'],'DQ_a OR Test[1]')
            self.assertIn('NOT',rows[0]['raw'])
            self.assertEqual(rows[0]['address']['raw'],'C1_I1_0')
            self.assertFalse(unknown)

    def test_unknown_and_wrong_direction_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'test.st.txt').write_text('a:=C1_DI99;\nC1_I1_00:=a;\n',encoding='utf-8')
            rows,unknown=parse_program(d)
            self.assertFalse(rows)
            self.assertEqual(len(unknown),2)


class Page:
    rect=SimpleNamespace(width=1000,height=800)
    def get_drawings(self):return []


class DrawingTests(unittest.TestCase):
    def words(self,title):
        return [(10,700,300,710,title,0,0,0), (100,80,180,90,'MB20-AI4-I',0,0,0),
                (100,100,180,110,'C1_1AI1I+',0,0,0),(100,120,180,130,'C1_1AI1-',0,0,0),
                (100,200,180,210,'C1_1AI2+',0,0,0),(650,100,800,110,'压力检测',0,0,0),
                (650,200,800,210,'液位检测',0,0,0)]
    def test_overview_layout_and_negative_terminal_deduplication(self):
        rows,_=station_page(Page(),0,self.words('MB20-AI4-I模块1总览图'))
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['function'],'压力检测')
        self.assertTrue(all(r['recognized'] for r in rows))
    def test_wiring_references_are_not_module_points(self):
        rows,refs=station_page(Page(),0,self.words('压力变送器现场接线'))
        self.assertFalse(rows)
        self.assertEqual(len(refs),2)

    def test_single_wrong_station_row_is_retained_as_duplicate_evidence(self):
        words=self.words('MB20-AI4-I模块1总览图')
        words[4]=(100,200,180,210,'D1_1AI2+',0,0,0)
        rows,_=station_page(Page(),0,words)
        self.assertEqual(len(rows),2)
        self.assertEqual({r['address']['station'] for r in rows},{'C1','D1'})


class MatchTests(unittest.TestCase):
    def sample(self):
        a=parse_address('C1_I1_00').to_dict()
        point={'address':a,'original_wire':'C1_I1_00','wire':'C1_I1_00','function':'信号名称甲','recognized':True,'page':1,'title':'模块总览图','void':''}
        row={'address':a,'var':'DI_信号名称乙','simple':True,'refs':1,'section':'test','line':1,'raw':'DI_信号名称乙:=NOT C1_I1_00;'}
        return {'points':[point]},[row]

    def test_fifty_percent_is_yellow_without_mandatory_judgment(self):
        data,program=self.sample()
        with patch('adaptive_workflow.similarity',return_value=.5):
            results,questions,decisions,_=compare(data,program,{})
        self.assertTrue(results[0]['status'].startswith('⚠'))
        self.assertFalse(questions)
        self.assertEqual(decisions[('test',1)]['program_token'],'C1_I1_00')

    def test_below_fifty_percent_remains_a_question(self):
        data,program=self.sample()
        with patch('adaptive_workflow.similarity',return_value=.4999):
            results,questions,decisions,_=compare(data,program,{})
        self.assertTrue(results[0]['status'].startswith('❓'))
        self.assertEqual(len(questions),1)
        self.assertFalse(decisions)

    def test_explicit_pending_overrides_yellow_threshold(self):
        data,program=self.sample()
        fb={'C1_I1_00|信号名称甲|DI_信号名称乙':{'judge':'待判断'}}
        with patch('adaptive_workflow.similarity',return_value=.9):
            results,questions,decisions,_=compare(data,program,fb)
        self.assertEqual(len(questions),1)
        self.assertFalse(decisions)

    def test_unique_displaced_candidate_above_threshold_is_yellow(self):
        data,program=self.sample()
        target=parse_address('C1_I2_00').to_dict()
        data['points'][0].update(address=target,wire='C1_I2_00',original_wire='C1_I2_00')
        with patch('adaptive_workflow.similarity',return_value=.6):
            results,questions,decisions,extra=compare(data,program,{})
        self.assertTrue(results[0]['status'].startswith('⚠'))
        self.assertFalse(questions)
        self.assertFalse(extra)
        self.assertEqual(decisions[('test',1)]['program_token'],'C1_I2_00')

    def test_tied_displaced_candidates_remain_a_question(self):
        data,program=self.sample()
        second={**program[0],'address':parse_address('C1_I1_01').to_dict(),'var':'DI_信号名称丙','line':2}
        program.append(second)
        target=parse_address('C1_I2_00').to_dict()
        data['points'][0].update(address=target,wire='C1_I2_00',original_wire='C1_I2_00')
        with patch('adaptive_workflow.similarity',return_value=.6):
            results,questions,decisions,_=compare(data,program,{})
        self.assertTrue(questions)
        self.assertFalse(decisions)
        self.assertIn('多个相同分数',results[0]['note'])

    def test_same_address_does_not_override_wrong_function(self):
        a=parse_address('C1_I1_00').to_dict()
        data={'points':[{'address':a,'original_wire':'C1_I1_00','wire':'C1_I1_00','function':'放卷停止按钮','recognized':True,'page':1,'title':'模块总览图','void':''}]}
        program=[{'address':a,'var':'DI_放卷启动按钮','simple':True,'refs':1,'section':'test','line':1,'raw':'DI_放卷启动按钮:=C1_I1_00;'}]
        results,questions,decisions,extra=compare(data,program,{})
        self.assertEqual(len(questions),1)
        self.assertFalse(decisions)
        self.assertFalse(extra)

    def test_opposite_meaning_stays_a_question_above_threshold(self):
        a=parse_address('C1_I1_00').to_dict()
        point={'address':a,'original_wire':'C1_I1_00','wire':'C1_I1_00','function':'气缸伸出位','recognized':True,'page':1,'title':'模块总览图','void':''}
        row={'address':a,'var':'DI_气缸缩回位','simple':True,'refs':1,'section':'test','line':1,'raw':'DI_气缸缩回位:=C1_I1_00;'}
        with patch('adaptive_workflow.similarity',return_value=.9):
            results,questions,decisions,_=compare({'points':[point]},[row],{})
        self.assertEqual(len(questions),1)
        self.assertFalse(decisions)
        self.assertIn('伸出/缩回',results[0]['note'])


class WorkflowTests(unittest.TestCase):
    def test_yellow_row_is_colored_and_not_a_mandatory_question(self):
        from openpyxl import load_workbook
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);old=root/'old';old.mkdir();out=root/'out';pdf=root/'drawing.pdf';pdf.write_bytes(b'fixture')
            raw='DI_放卷气压低报警:=NOT C1_I1_00;'
            (old/'test.st.txt').write_text(raw+'\n',encoding='utf-8')
            a=parse_address('C1_I1_00').to_dict()
            data={'pdf_sha256':'fixture','points':[{'address':a,'original_wire':'C1_I1_00','wire':'C1_I1_00','function':'放卷压力报警','recognized':True,'page':1,'title':'模块总览图','void':''}], 'references':{},'unrecognized_pages':[]}
            with patch('adaptive_workflow.extract',return_value=data):
                status=run(pdf,old,out)
            self.assertEqual(status['yellow'],1)
            self.assertEqual(status['questions'],0)
            self.assertIn(raw,(out/'新程序段/test.st.txt').read_text(encoding='utf-8'))
            wb=load_workbook(out/'对点报告.xlsx')
            self.assertTrue(wb['对点结果']['A2'].fill.fgColor.rgb.endswith('FFF2CC'))
            self.assertEqual(wb['对点结果']['K2'].number_format,'0.0%')
            wb.close()

    def test_padding_equivalence_does_not_rename_program_variable_implicitly(self):
        source=parse_address('C1_I1_0').to_dict();target=parse_address('C1_I01_00').to_dict()
        point={'address':target,'original_wire':'C1_I01_00','wire':'C1_I1_00','function':'停止按钮','recognized':True,'page':1,'title':'模块总览图','void':''}
        row={'address':source,'var':'DI_停止按钮','simple':True,'refs':1,'section':'test','line':1,'raw':'DI_停止按钮:=C1_I1_0;'}
        results,questions,decisions,_=compare({'points':[point]},[row],{})
        self.assertFalse(questions)
        self.assertEqual(results[0]['wire'],'C1_I1_0')
        self.assertEqual(decisions[('test',1)]['program_token'],'C1_I1_0')
    def test_complete_unique_match_changes_address_but_preserves_not(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);old=root/'old';old.mkdir();out=root/'out';pdf=root/'drawing.pdf';pdf.write_bytes(b'fixture')
            (old/'test.st.txt').write_text('//====== ST块 1 ======\nDI_放卷停止按钮:=NOT C1_I1_00;\n',encoding='utf-8')
            a=parse_address('C1_I2_02').to_dict()
            data={'pdf_sha256':'fixture','points':[{'address':a,'original_wire':'C1_I2_02','wire':'C1_I2_02','function':'放卷停止按钮','recognized':True,'page':1,'title':'DI模块总览图','void':''}], 'references':{},'unrecognized_pages':[]}
            with patch('adaptive_workflow.extract',return_value=data):
                status=run(pdf,old,out)
            self.assertTrue(status['ready'])
            self.assertEqual(status['changes'],1)
            self.assertIn('DI_放卷停止按钮:=NOT C1_I2_02;', (out/'新程序段/test.st.txt').read_text(encoding='utf-8'))
            self.assertIn('C1_I1_00',(old/'test.st.txt').read_text(encoding='utf-8'))

    def test_review_manifest_blocks_actual_clipboard_write_before_opening_raw_data(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);prog=root/'prog';prog.mkdir()
            (prog/'迁移状态.json').write_text(json.dumps({'ready':False}),encoding='utf-8')
            writer=IO.parent/'sysmac-read/write_sysmac_clipboard.py'
            result=subprocess.run([sys.executable,str(writer),str(root/'missing.bin'),str(prog)],capture_output=True,env={**__import__('os').environ,'PYTHONIOENCODING':'utf-8'},encoding='utf-8')
            self.assertNotEqual(result.returncode,0)
            self.assertIn('核对稿',result.stderr)
            self.assertNotIn('FileNotFoundError',result.stderr)

    def test_edited_generated_source_blocks_export_even_with_old_ready_flag(self):
        from adaptive_drawing import fingerprint
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);old=root/'old';old.mkdir();prog=root/'prog';prog.mkdir()
            (old/'test.st.txt').write_text('a:=C1_I1_00;',encoding='utf-8')
            (prog/'test.st.txt').write_text('a:=C1_I1_01;',encoding='utf-8')
            manifest={'ready':True,'source_hashes':{'test.st.txt':fingerprint(old/'test.st.txt')},
                      'output_hashes':{'test.st.txt':'stale-hash'}}
            (prog/'迁移状态.json').write_text(json.dumps(manifest),encoding='utf-8')
            writer=IO.parent/'sysmac-read/write_sysmac_clipboard.py'
            result=subprocess.run([sys.executable,str(writer),str(old/'missing.bin'),str(prog)],capture_output=True,
                                  env={**__import__('os').environ,'PYTHONIOENCODING':'utf-8'},encoding='utf-8')
            self.assertNotEqual(result.returncode,0)
            self.assertIn('程序或变量文件已变化',result.stderr)
    def test_unique_exact_meaning_can_move_within_station(self):
        source=parse_address('C1_I1_00').to_dict()
        target=parse_address('C1_I2_02').to_dict()
        point={'address':target,'original_wire':'C1_I2_02','wire':'C1_I2_02','function':'放卷停止按钮','recognized':True,'page':1,'title':'模块总览图','void':''}
        row={'address':source,'var':'DI_放卷停止按钮','simple':True,'refs':1,'section':'test','line':1,'raw':'DI_放卷停止按钮:=NOT C1_I1_00;'}
        results,questions,decisions,extra=compare({'points':[point]},[row],{})
        self.assertFalse(questions)
        self.assertFalse(extra)
        self.assertEqual(decisions[('test',1)]['address']['key'],target['key'])

    def test_no_automatic_migration_between_stations(self):
        source=parse_address('C1_I1_00').to_dict();target=parse_address('D1_I1_00').to_dict()
        point={'address':target,'original_wire':'D1_I1_00','wire':'D1_I1_00','function':'停止按钮','recognized':True,'page':1,'title':'模块总览图','void':''}
        row={'address':source,'var':'DI_停止按钮','simple':True,'refs':1,'section':'test','line':1,'raw':'DI_停止按钮:=C1_I1_00;'}
        _,questions,decisions,_=compare({'points':[point]},[row],{})
        self.assertTrue(questions)
        self.assertFalse(decisions)


if __name__=='__main__':unittest.main()
