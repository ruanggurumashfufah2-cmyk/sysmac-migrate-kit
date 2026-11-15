"""Bounded temporary HTTP session: exercises generated reports, never clipboard data."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stdout
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts/sysmac-io-check'))
from adaptive_workflow import run
from io_address import parse_address
spec=importlib.util.spec_from_file_location('adaptive_workbench',ROOT/'scripts/workbench/server.py')
server=importlib.util.module_from_spec(spec);spec.loader.exec_module(server)


class WorkbenchAdaptiveTests(unittest.TestCase):
    def test_bulk_feedback_is_atomic_preserves_other_decisions_and_can_undo(self):
        with tempfile.TemporaryDirectory() as d:
            project=Path(d)/'项目'/'批量测试';project.mkdir(parents=True)
            original_root=server.ROOT;server.ROOT=d
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.H)
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{http.server_port}/api/p/'+urllib.parse.quote('批量测试')
            def post(body):
                req=urllib.request.Request(base+'/feedback_batch',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=10) as r:return json.load(r)
            path=project/'输出/对点反馈.json'
            try:
                server.save_feedback(str(path),[{'wire':'原判断','judge':'保留','remark':'不能丢'}])
                items=[{'wire':f'@review:{i}','judge':json.dumps({'action':'new','values':{'var':f'DI_测试{i}','section':'输入'}})} for i in range(600)]
                result=post({'items':items});self.assertEqual(result,{'ok':True,'count':601,'saved':600})
                before=path.read_bytes()
                with self.assertRaises(urllib.error.HTTPError) as caught:post({'items':[{'wire':'不能部分写入','judge':'保留'},{'wire':'bad','judge':42}]})
                self.assertEqual(caught.exception.code,400);self.assertEqual(path.read_bytes(),before)
                result=post({'items':[{'wire':f'@review:{i}'} for i in range(600)]})
                self.assertEqual(result['count'],1);self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['原判断||']['remark'],'不能丢')
            finally:
                http.shutdown();http.server_close();thread.join(timeout=5);server.ROOT=original_root

    def test_report_status_feedback_and_clipboard_guard(self):
        with tempfile.TemporaryDirectory() as d:
            project=Path(d)/'项目'/'中文测试';old=project/'旧程序';old.mkdir(parents=True)
            pdf=project/'drawing.pdf';pdf.write_bytes(b'fixture')
            (old/'test.st.txt').write_text('DI_启动:=C1_I1_00;\n',encoding='utf-8')
            a=parse_address('C1_I1_00').to_dict()
            data={'pdf_sha256':'fixture','points':[{'address':a,'original_wire':'C1_I1_00','wire':'C1_I1_00','function':'停止','recognized':True,'page':1,'title':'模块总览图','void':''}], 'references':{},'unrecognized_pages':[]}
            with patch('adaptive_workflow.extract',return_value=data),redirect_stdout(io.StringIO()):
                run(pdf,old,project/'输出')
            original_root=server.ROOT;server.ROOT=d
            http=server.ThreadingHTTPServer(('127.0.0.1',0),server.H)
            thread=threading.Thread(target=http.serve_forever,daemon=True);thread.start()
            base=f'http://127.0.0.1:{http.server_port}/api/p/'+urllib.parse.quote('中文测试')
            def api(path,body=None):
                req=urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,
                                           headers={'Content-Type':'application/json'})
                with urllib.request.urlopen(req,timeout=10) as r:return json.load(r)
            try:
                self.assertFalse(api('/status')['migration']['ready'])
                for kind in server.TABLES:
                    self.assertIn('rows',api('/table/'+kind))
                body={'wire':'C1_I1_00','function':'停止','var':'DI_启动','judge':'待判断','remark':'核对启动停止'}
                self.assertEqual(api('/feedback',body)['count'],1)
                table=api('/table/results')
                self.assertEqual(table['rows'][0][table['columns'].index('备注')],'核对启动停止')
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    api('/write_clipboard',{})
                self.assertEqual(caught.exception.code,400)
                self.assertIn('核对稿',json.load(caught.exception)['log'])
                pending=api('/table/questions')
                task=json.loads(pending['rows'][0][pending['columns'].index('处理数据')])
                key='@review:'+task['id']
                api('/feedback',{'wire':key,'judge':json.dumps({'action':'confirm','values':{}}),'remark':'通过页面处理'})
                with patch('adaptive_workflow.extract',return_value=data),redirect_stdout(io.StringIO()):
                    run(pdf,old,project/'输出')
                self.assertEqual(api('/table/questions')['rows'],[])
                self.assertEqual(api('/status')['counts']['待判断'],0)
                self.assertIn('DI_启动:=C1_I1_00;', (project/'输出/新程序段/test.st.txt').read_text(encoding='utf-8'))
                api('/feedback',{'wire':key})
                with patch('adaptive_workflow.extract',return_value=data),redirect_stdout(io.StringIO()):
                    run(pdf,old,project/'输出')
                self.assertEqual(len(api('/table/questions')['rows']),1)
            finally:
                http.shutdown();http.server_close();thread.join(timeout=5);server.ROOT=original_root


if __name__=='__main__':unittest.main()
