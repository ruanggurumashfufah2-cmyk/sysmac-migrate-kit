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
from adaptive_workflow import run
from io_address import parse_address
from openpyxl import load_workbook


class ReviewActionsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.old=self.root/'旧程序';self.old.mkdir()
        self.out=self.root/'输出';self.pdf=self.root/'图纸.pdf';self.pdf.write_bytes(b'fixture')
    def source(self,text):
        (self.old/'test.st.txt').write_text('//====== ST块 1 ======\n'+text,encoding='utf-8')
    def point(self,wire,function,page=1):
        a=parse_address(wire)
        return {'address':a.to_dict(),'wire':a.program_token(),'original_wire':wire,'function':function,
                'recognized':True,'page':page,'title':'DI模块总览图','void':''}
    def execute(self,points):
        data={'pdf_sha256':'fixture','points':points,'references':{},'unrecognized_pages':[]}
        with patch('adaptive_workflow.extract',return_value=data),redirect_stdout(io.StringIO()):
            return run(self.pdf,self.old,self.out)
    def tasks(self):
        wb=load_workbook(self.out/'新程序段/待判断.xlsx',read_only=True,data_only=True)
        rows=list(wb.active.values);i=rows[0].index('处理数据')
        tasks=[json.loads(r[i]) for r in rows[1:]];wb.close();return tasks
    def save(self,task,action,values=None):
        p=self.out/'对点反馈.json';fb=json.loads(p.read_text(encoding='utf-8')) if p.exists() else {}
        key='@review:'+task['id']+'||'
        if action is None:fb.pop(key,None)
        else:fb[key]={'judge':json.dumps({'action':action,'values':values or {}}),'remark':'测试判断'}
        p.write_text(json.dumps(fb,ensure_ascii=False),encoding='utf-8')
    def generated(self):return (self.out/'新程序段/test.st.txt').read_text(encoding='utf-8')

    def test_confirm_then_undo_really_recomputes_question(self):
        self.source('DI_启动:=NOT C1_I1_00;\n');points=[self.point('C1_I1_00','停止')]
        self.assertEqual(self.execute(points)['questions'],1)
        task=self.tasks()[0];self.assertIn('confirm',[o['value'] for o in task['options']])
        self.save(task,'confirm');self.assertEqual(self.execute(points)['questions'],0)
        self.assertIn('DI_启动:=NOT C1_I1_00;',self.generated())
        self.save(task,None);self.assertEqual(self.execute(points)['questions'],1)

    def test_unused_comments_statement_and_fills_empty_st_block(self):
        self.source('DI_启动:=C1_I1_00;\n');points=[self.point('C1_I1_00','停止')]
        self.execute(points);self.save(self.tasks()[0],'unused');s=self.execute(points)
        self.assertEqual(s['questions'],0);self.assertEqual(s['audit'],0)
        self.assertIn('//DI_启动:=C1_I1_00;',self.generated())
        self.assertIn('空块占位:=FALSE;',self.generated())

    def test_new_mapping_generates_both_declaration_and_code(self):
        self.source('DI_备用:=C1_I1_00;\n');points=[self.point('C1_I1_00','停止按钮')]
        self.execute(points);self.save(self.tasks()[0],'new',{'var':'DI_停止按钮','section':'test'})
        s=self.execute(points)
        self.assertEqual(s['questions'],0);self.assertEqual(s['audit'],0)
        self.assertIn('DI_停止按钮:=C1_I1_00;',self.generated())
        self.assertIn('//DI_备用:=C1_I1_00;',self.generated())
        self.assertIn('DI_停止按钮\tBOOL',(self.out/'新程序段/新增变量.txt').read_text(encoding='utf-8'))

    def test_defer_preserves_original_problem_category_and_reason(self):
        self.source('DI_启动:=C1_I1_00;\nDI_备用:=C1_I1_01;\nC1_Q1_00:=DQ_风机启动;\nC1_Q1_00:=DQ_风机停止;\n')
        points=[self.point('C1_I1_00','停止'),self.point('C1_I1_01','停止按钮'),
                self.point('C1_I2_00','新增加热完成'),self.point('C1_Q1_00','风机请求')]
        self.execute(points);before={t['id']:t for t in self.tasks()}
        for task in before.values():self.save(task,'defer')
        self.execute(points);after={t['id']:t for t in self.tasks()}
        self.assertEqual(set(before),set(after))
        for identifier,old_task in before.items():
            self.assertEqual(after[identifier]['problem_group'],old_task['problem_group'])
            self.assertEqual(after[identifier]['problem_reason'],old_task['problem_reason'])
            self.assertIn('暂缓',after[identifier]['reason'])
        self.assertEqual({t['problem_group'] for t in after.values()},{'direction','spare','missing','duplicate'})

    def test_choose_one_duplicate_output_removes_other_assignment_and_audit(self):
        self.source('C1_Q1_00:=DQ_风机启动 OR Test[1];\nC1_Q1_00:=DQ_加热启动;\n')
        points=[self.point('C1_Q1_00','风机启动')]
        self.execute(points);task=self.tasks()[0]
        self.assertEqual(len(task['sources']),2)
        self.save(task,'source',{'source':{'section':'test','line':2}})
        s=self.execute(points)
        self.assertEqual(s['questions'],0);self.assertEqual(s['audit'],0)
        self.assertIn('C1_Q1_00:=DQ_风机启动 OR Test[1];',self.generated())
        self.assertIn('//C1_Q1_00:=DQ_加热启动;',self.generated())

    def test_module_keep_and_comment_are_applied_to_actual_code(self):
        self.source('DI_停止:=C1_I1_00;\nDI_急停:=C1_I2_00;\n')
        points=[self.point('C1_I1_00','停止')]
        self.execute(points);task=self.tasks()[0];self.assertEqual(task['kind'],'module')
        self.save(task,'keep_module');self.assertEqual(self.execute(points)['questions'],0)
        self.assertIn('\nDI_急停:=C1_I2_00;',self.generated())
        self.save(task,'comment_module');self.assertEqual(self.execute(points)['questions'],0)
        self.assertIn('//DI_急停:=C1_I2_00;',self.generated())
        self.assertIn('\nDI_停止:=C1_I1_00;',self.generated())

    def test_wrong_type_source_and_illegal_new_name_do_not_clear_task(self):
        self.source('C1_Q1_00:=DQ_风机启动;\nDI_备用:=C1_I1_00;\n')
        points=[self.point('C1_Q1_00','风机启动'),self.point('C1_I1_00','停止')]
        self.execute(points);task=self.tasks()[0]
        self.save(task,'source',{'source':{'section':'test','line':2}})
        self.assertEqual(self.execute(points)['questions'],1)
        self.assertIn('类型不一致',self.tasks()[0]['reason'])
        self.save(task,'new',{'var':'bad name','section':'../../bad'})
        self.assertEqual(self.execute(points)['questions'],1)
        self.assertIn('新变量名',self.tasks()[0]['reason'])
        self.assertIn('\nDI_备用:=C1_I1_00;',self.generated())

    def test_source_revision_invalidates_old_decisions(self):
        self.source('DI_启动:=C1_I1_00;\n');points=[self.point('C1_I1_00','停止')]
        self.execute(points);task=self.tasks()[0];self.save(task,'confirm')
        self.assertEqual(self.execute(points)['questions'],0)
        self.source('DI_另外一个启动:=C1_I1_00;\n')
        self.assertEqual(self.execute(points)['questions'],1)
        self.assertNotEqual(self.tasks()[0]['id'],task['id'])

    def test_correcting_duplicate_drawing_address_resolves_both_rows(self):
        self.source('C2_Q1_08:=DQ_放卷B轴夹头电磁阀;\nD2_Q1_08:=DQ_收卷B轴夹头电磁阀;\n')
        points=[self.point('C2_Q1_08','放卷B轴夹头电磁阀',1),self.point('C2_Q1_08','收卷B轴夹头电磁阀',2)]
        self.execute(points);task=next(t for t in self.tasks() if t['page']==2)
        self.save(task,'address',{'address':'D2_Q1_08'})
        self.assertEqual(self.execute(points)['questions'],0)
        self.assertIn('D2_Q1_08:=DQ_收卷B轴夹头电磁阀;',self.generated())

    def test_batch_new_names_reject_case_insensitive_collisions(self):
        self.source('DI_Exists:=C1_I1_00;\n')
        points=[self.point('C1_I1_00','Exists'),self.point('C1_I1_01','新检测一'),self.point('C1_I1_02','新检测二')]
        self.execute(points)
        tasks=self.tasks()
        self.save(tasks[0],'new',{'var':'DI_New','section':'test'})
        self.save(tasks[1],'new',{'var':'di_new','section':'test'})
        self.assertEqual(self.execute(points)['questions'],2)
        self.assertTrue(all('相同变量名' in t['reason'] for t in self.tasks()))
        for task in self.tasks():self.save(task,'new',{'var':'di_exists','section':'test'})
        self.assertEqual(self.execute(points)['questions'],2)
        self.assertTrue(all('变量已存在' in t['reason'] for t in self.tasks()))
        for i,task in enumerate(self.tasks()):self.save(task,'new',{'var':f'DI_新增{i}','section':'test'})
        self.assertEqual(self.execute(points)['questions'],0)
        self.assertIn('DI_新增0:=C1_I1_01;',self.generated())
        self.assertIn('DI_新增1:=C1_I1_02;',self.generated())


if __name__=='__main__':unittest.main()
