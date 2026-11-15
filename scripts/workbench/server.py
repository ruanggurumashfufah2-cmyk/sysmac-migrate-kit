"""Sysmac 迁移工作台：本地网页 + JSON API（人和任何 AI agent 共用）

启动:  python server.py [--root 工作目录] [--port 8765] [--no-browser]
       工作目录下的 项目/<项目名>/ 存放：图纸/*.pdf、旧程序/、输出/
网页:  http://127.0.0.1:8765
API:   见 GET /api （返回接口清单），所有接口只监听本机 127.0.0.1
"""
import argparse
import ctypes
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))


def find_scripts():
    for c in (os.path.join(HERE, '..'), os.path.join(HERE, '..', '..')):
        if os.path.exists(os.path.join(c, 'sysmac-io-check', 'run_all.py')):
            return os.path.abspath(c)
    sys.exit('找不到 sysmac-io-check 脚本目录')


SCRIPTS = find_scripts()
IO = os.path.join(SCRIPTS, 'sysmac-io-check')
RD = os.path.join(SCRIPTS, 'sysmac-read')
ROOT = os.getcwd()
JOBS = {}          # 项目名 -> {running, log, rc, step, started}
LOCK = threading.Lock()

API_DOC = {
    'GET  /api/projects': '项目列表',
    'POST /api/projects {"name"}': '新建项目',
    'GET  /api/p/<项目>/status': '项目状态与统计',
    'POST /api/p/<项目>/read_clipboard': '读取 Sysmac 剪贴板里的旧程序段（用户先在 Sysmac 复制）',
    'POST /api/p/<项目>/drawing?filename=x.pdf  (body=PDF 字节)': '上传图纸',
    'POST /api/p/<项目>/drawing_path {"path"}': '指定本机已有的图纸 PDF 路径（复制到项目里）',
    'POST /api/p/<项目>/run {"full":true|false}': '对点+生成+清算（full=false 时复用已提取的点表）',
    'GET  /api/p/<项目>/job': '后台任务进度和日志',
    'GET  /api/p/<项目>/table/<results|doubts|iomap|extra|questions|audit|changes|newvars>': '表格数据',
    'POST /api/p/<项目>/feedback {"wire","function","var","judge","remark"}': '保存一条判断（wire 为图纸原线号如 I-A01-00）',
    'POST /api/p/<项目>/feedback_batch {"items":[...]}': '批量保存判断（全部校验后一次写入，不直接生成程序）',
    'GET  /api/p/<项目>/sections': '新程序段列表',
    'GET  /api/p/<项目>/section/<段名>': '某段新旧程序全文',
    'POST /api/p/<项目>/write_clipboard': '把新程序段放入 Sysmac 剪贴板（之后在 Sysmac 右键 POUs 粘贴多个项目）',
    'POST /api/p/<项目>/copy_newvars': '把 新增变量.txt 复制到剪贴板（粘贴到全局变量表）',
    'POST /api/p/<项目>/open_folder': '在资源管理器打开输出目录',
    'GET  /api/rules': '迁移原则（Markdown）',
}


# ---------------- 路径 ----------------
def pdir(name):
    if not re.fullmatch(r'[^\\/:*?"<>|]{1,60}', name or '') or name in ('.', '..'):
        raise ValueError('项目名不合法')
    return os.path.join(ROOT, '项目', name)


def paths(name):
    d = pdir(name)
    return {'dir': d, 'old': os.path.join(d, '旧程序'), 'pdfdir': os.path.join(d, '图纸'),
            'out': os.path.join(d, '输出'), 'prog': os.path.join(d, '输出', '新程序段'),
            'csv': os.path.join(d, '输出', '图纸点表.csv'), 'fb': os.path.join(d, '输出', '对点反馈.json')}


def latest(pattern):
    fs = [f for f in glob.glob(pattern) if not os.path.basename(f).startswith('~$')]
    return max(fs, key=os.path.getmtime) if fs else None


def pdf_of(name):
    p = paths(name)
    return latest(os.path.join(p['pdfdir'], '*.pdf')) or latest(os.path.join(p['dir'], '*.pdf'))


def env():
    return {**os.environ, 'PYTHONIOENCODING': 'utf-8'}


def save_feedback(path,items):
    """Validate a whole batch before mutating the shared feedback file."""
    if not isinstance(items,list) or not 1<=len(items)<=2000:
        raise ValueError('批量处理需要 1～2000 条判断')
    prepared=[]
    for b in items:
        if not isinstance(b,dict):raise ValueError('判断格式不正确')
        for field in ('wire','function','var','judge','remark'):
            if not isinstance(b.get(field,''),str):raise ValueError('判断字段必须是文字')
        key='|'.join(b.get(k,'') for k in ('wire','function','var'))
        if not b.get('wire'):raise ValueError('判断缺少线号或任务编号')
        prepared.append((key,{'judge':b.get('judge','').strip(),'remark':b.get('remark','').strip()}))
    with LOCK:
        fb={}
        if os.path.exists(path):
            with open(path,encoding='utf-8') as f:fb=json.load(f)
        for key,item in prepared:
            if item['judge'] or item['remark']:fb[key]=item
            else:fb.pop(key,None)
        os.makedirs(os.path.dirname(path),exist_ok=True)
        with open(path+'.tmp','w',encoding='utf-8') as f:json.dump(fb,f,ensure_ascii=False,indent=1)
        os.replace(path+'.tmp',path)
    return {'ok':True,'count':len(fb),'saved':len(prepared)}


def run_py(script, *args, timeout=1800):
    r = subprocess.run([sys.executable, '-W', 'ignore', script, *args], capture_output=True,
                       env=env(), timeout=timeout)
    out = r.stdout.decode('utf-8', 'replace') + r.stderr.decode('utf-8', 'replace')
    return r.returncode, out


# ---------------- 后台任务 ----------------
def start_job(name, steps):
    with LOCK:
        if JOBS.get(name, {}).get('running'):
            raise RuntimeError('该项目已有任务在运行')
        job = JOBS[name] = {'running': True, 'log': '', 'rc': None, 'step': '', 'started': time.time()}

    def work():
        rc = 0
        try:
            for title, script, args in steps:
                job['step'] = title
                job['log'] += f'\n>>> {title}\n'
                with subprocess.Popen([sys.executable, '-W', 'ignore', script, *args], stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, env=env()) as p:
                    for line in p.stdout:
                        job['log'] += line.decode('utf-8', 'replace')
                    rc = p.wait()
                if rc:
                    job['log'] += f'\n【失败】{title} 返回 {rc}，请看上方具体原因。\n'
                    break
        except Exception as e:
            rc = 1
            job['log'] += f'\n【失败】{e}\n'
        finally:
            job.update(running=False, rc=rc, step='完成' if rc == 0 else '失败')
    threading.Thread(target=work, daemon=True).start()
    return job


# ---------------- 表格 ----------------
TABLES = {
    'results': ('report', '对点结果'), 'doubts': ('report', '图纸疑点'), 'iomap': ('report', 'IO映射粘贴'),
    'extra': ('report', '程序有图纸无'), 'questions': ('prog', '待判断.xlsx:待判断'),
    'audit': ('prog', '清算报告.xlsx:清算'), 'changes': ('prog', '变更清单.xlsx:变更清单'),
    'newvars': ('prog', '新增变量.xlsx:新增变量'),
}


def read_sheet(path, sheet):
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet not in wb.sheetnames:
            return {'columns': [], 'rows': []}
        it = wb[sheet].iter_rows(values_only=True)
        cols = [c if c is not None else '' for c in next(it, [])]
        rows = []
        for r in it:
            if not any(v not in (None, '') for v in r):
                continue
            rows.append(['' if v is None else v for v in r])
        return {'columns': cols, 'rows': rows, 'file': os.path.basename(path)}
    finally:
        wb.close()


def table(name, kind):
    p = paths(name)
    where, spec = TABLES[kind]
    if where == 'report':
        f = latest(os.path.join(p['out'], '对点报告*.xlsx'))
        result = read_sheet(f, spec) if f else {'columns': [], 'rows': []}
        if kind=='results' and result['rows'] and os.path.exists(p['fb']):
            with open(p['fb'],encoding='utf-8') as fb_file:
                feedback=json.load(fb_file)
            cols=result['columns']
            for row in result['rows']:
                key='|'.join(str(row[cols.index(c)] or '') for c in ('图纸原线号','图纸功能','程序变量'))
                if key in feedback:
                    item=feedback[key]
                    row[cols.index('你的判断')]=item.get('judge','')
                    row[cols.index('备注')]=item.get('remark','')
        return result
    fn, sheet = spec.split(':')
    f = latest(os.path.join(p['prog'], fn.replace('.xlsx', '*.xlsx')))
    return read_sheet(f, sheet) if f else {'columns': [], 'rows': []}


# ---------------- 剪贴板文本 ----------------
def set_clipboard_text(text):
    from ctypes import wintypes as w
    u, k = ctypes.WinDLL('user32'), ctypes.WinDLL('kernel32')
    u.SetClipboardData.argtypes = [w.UINT, w.HANDLE]
    u.SetClipboardData.restype = w.HANDLE
    k.GlobalAlloc.restype = w.HGLOBAL
    k.GlobalAlloc.argtypes = [w.UINT, ctypes.c_size_t]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [w.HGLOBAL]
    k.GlobalUnlock.argtypes = [w.HGLOBAL]
    u.OpenClipboard.argtypes = [w.HWND]
    data = text.encode('utf-16-le') + b'\x00\x00'
    h = k.GlobalAlloc(0x0002, len(data))
    p = k.GlobalLock(h)
    ctypes.memmove(p, data, len(data))
    k.GlobalUnlock(h)
    for _ in range(10):
        if u.OpenClipboard(None):
            break
        time.sleep(0.1)
    else:
        raise RuntimeError('剪贴板被占用')
    try:
        u.EmptyClipboard()
        u.SetClipboardData(13, h)
    finally:
        u.CloseClipboard()


# ---------------- 状态 ----------------
def status(name, light=False):
    p = paths(name)
    st = {'name': name, 'dir': p['dir'], 'pdf': pdf_of(name), 'job': JOBS.get(name)}
    summ = os.path.join(p['old'], 'summary.txt')
    st['old_sections'] = len(open(summ, encoding='utf-8').read().splitlines()) if os.path.exists(summ) else 0
    st['has_output'] = os.path.exists(p['prog'])
    migration = os.path.join(p['prog'], '迁移状态.json')
    if os.path.exists(migration):
        with open(migration,encoding='utf-8') as f:
            st['migration'] = json.load(f)
    if light:                       # 项目列表只要轻量信息，不读报告
        st.pop('job')
        return st
    st['report'] = os.path.basename(latest(os.path.join(p['out'], '对点报告*.xlsx')) or '') or None
    counts = {}
    try:
        res = table(name, 'results')
        if res['rows']:
            from collections import Counter
            counts['对点结果'] = dict(Counter(r[0] for r in res['rows']))
        for k, lab in (('questions', '待判断'), ('audit', '清算'), ('newvars', '新增变量'), ('changes', '改动')):
            t = table(name, k)
            counts[lab] = len(t['rows'])
        a = table(name, 'audit')
        if a['rows']:
            from collections import Counter
            counts['清算分类'] = dict(Counter(r[0] for r in a['rows']))
        c = table(name, 'changes')
        if c['rows']:
            from collections import Counter
            counts['改动分类'] = dict(Counter(r[2] for r in c['rows']))
    except Exception as e:  # 报告被占用等
        counts['错误'] = str(e)
    st['counts'] = counts
    fb = p['fb']
    st['feedback'] = 0
    if os.path.exists(fb):
        with open(fb,encoding='utf-8') as feedback_file:st['feedback']=len(json.load(feedback_file))
    return st


# ---------------- HTTP ----------------
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj, ctype='application/json; charset=utf-8'):
        body = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def body(self):
        n = int(self.headers.get('Content-Length') or 0)
        return self.rfile.read(n) if n else b''

    def jbody(self):
        b = self.body()
        return json.loads(b.decode('utf-8')) if b else {}

    def route(self, method):
        u = urllib.parse.urlparse(self.path)
        parts = [urllib.parse.unquote(x) for x in u.path.strip('/').split('/') if x]
        q = dict(urllib.parse.parse_qsl(u.query))
        try:
            if method == 'GET' and (not parts or parts == ['index.html']):
                return self.send(200, open(os.path.join(HERE, 'index.html'), 'rb').read(), 'text/html; charset=utf-8')
            if parts == ['api']:
                return self.send(200, API_DOC)
            if parts == ['api', 'rules']:
                f = next((x for x in [os.path.join(SCRIPTS, '..', '迁移原则.md'),
                                      os.path.join(SCRIPTS, 'sysmac-migrate', '迁移原则.md')] if os.path.exists(x)), None)
                return self.send(200, {'markdown': open(f, encoding='utf-8').read() if f else ''})
            if parts == ['api', 'projects']:
                if method == 'GET':
                    d = os.path.join(ROOT, '项目')
                    names = sorted(os.listdir(d)) if os.path.isdir(d) else []
                    return self.send(200, [status(n, light=True) for n in names if os.path.isdir(os.path.join(d, n))])
                name = self.jbody().get('name', '').strip()
                p = paths(name)
                for k in ('old', 'pdfdir', 'out'):
                    os.makedirs(p[k], exist_ok=True)
                return self.send(200, status(name))
            if len(parts) >= 4 and parts[:2] == ['api', 'p']:
                return self.project(method, parts[2], parts[3], parts[4:], q)
            return self.send(404, {'error': '没有这个接口，GET /api 查看清单'})
        except Exception as e:
            return self.send(400, {'error': str(e)})

    def project(self, method, name, act, rest, q):
        p = paths(name)
        if not os.path.isdir(p['dir']):
            return self.send(404, {'error': f'项目 {name} 不存在'})
        if act == 'status':
            return self.send(200, status(name))
        if act == 'job':
            return self.send(200, JOBS.get(name) or {'running': False, 'log': '', 'rc': None})
        if act == 'table':
            return self.send(200, table(name, rest[0]))
        if act == 'sections':
            fs = sorted(f[:-7] for f in os.listdir(p['prog']) if f.endswith('.st.txt')) if os.path.isdir(p['prog']) else []
            return self.send(200, fs)
        if act == 'section':
            sec = rest[0]
            rd = lambda d: open(os.path.join(d, sec + '.st.txt'), encoding='utf-8').read() \
                if os.path.exists(os.path.join(d, sec + '.st.txt')) else ''
            return self.send(200, {'name': sec, 'old': rd(p['old']), 'new': rd(p['prog'])})
        if method != 'POST':
            return self.send(405, {'error': '需要 POST'})
        if act == 'read_clipboard':
            rc, out = run_py(os.path.join(RD, 'read_sysmac_clipboard.py'), p['old'])
            return self.send(200 if rc == 0 else 400, {'ok': rc == 0, 'log': out})
        if act == 'drawing':
            fn = os.path.basename(q.get('filename') or 'drawing.pdf')
            if not fn.lower().endswith('.pdf'):
                return self.send(400, {'error': '只接受 PDF'})
            os.makedirs(p['pdfdir'], exist_ok=True)
            with open(os.path.join(p['pdfdir'], fn), 'wb') as f:
                f.write(self.body())
            return self.send(200, {'ok': True, 'pdf': fn})
        if act == 'drawing_path':
            src = self.jbody().get('path', '')
            if not (os.path.isfile(src) and src.lower().endswith('.pdf')):
                return self.send(400, {'error': '找不到这个 PDF'})
            import shutil
            os.makedirs(p['pdfdir'], exist_ok=True)
            shutil.copy2(src, p['pdfdir'])
            return self.send(200, {'ok': True, 'pdf': os.path.basename(src)})
        if act == 'run':
            full = self.jbody().get('full', not os.path.exists(p['csv']))
            pdf = pdf_of(name)
            if not os.path.exists(os.path.join(p['old'], 'clipboard_raw.bin')):
                return self.send(400, {'error': '还没读取旧程序（第 1 步）'})
            if not pdf:
                return self.send(400, {'error': '还没有图纸 PDF（第 2 步）'})
            os.makedirs(p['out'], exist_ok=True)
            args=[pdf,p['old'],p['out']] + ([] if full else ['--reuse'])
            steps=[('自动识别格式、对点与生成',os.path.join(IO,'run_all.py'),args)]
            start_job(name, steps)
            return self.send(200, {'ok': True, 'started': [s[0] for s in steps]})
        if act == 'feedback':
            b = self.jbody()
            return self.send(200,save_feedback(p['fb'],[b]))
        if act == 'feedback_batch':
            return self.send(200,save_feedback(p['fb'],self.jbody().get('items')))
        if act == 'write_clipboard':
            rc, out = run_py(os.path.join(RD, 'write_sysmac_clipboard.py'), os.path.join(p['old'], 'clipboard_raw.bin'),
                             p['prog'], '--types', os.path.join(p['prog'], '新增变量.txt'))
            return self.send(200 if rc == 0 else 400, {'ok': rc == 0, 'log': out})
        if act == 'copy_newvars':
            f = os.path.join(p['prog'], '新增变量.txt')
            set_clipboard_text(open(f, encoding='utf-8-sig').read())
            return self.send(200, {'ok': True})
        if act == 'open_folder':
            os.startfile(p['out'] if os.path.isdir(p['out']) else p['dir'])
            return self.send(200, {'ok': True})
        return self.send(404, {'error': '没有这个接口'})

    def do_GET(self):
        self.route('GET')

    def do_POST(self):
        self.route('POST')


def main():
    global ROOT
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default=os.getcwd())
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args()
    ROOT = os.path.abspath(a.root.strip().strip('"').rstrip('\\/') or '.')   # bat 里 "%~dp0" 末尾的 \" 会多出引号
    os.makedirs(os.path.join(ROOT, '项目'), exist_ok=True)
    url = f'http://127.0.0.1:{a.port}'
    try:                                   # 已有工作台在运行 → 直接打开浏览器（Windows 允许端口复用，不能靠绑定失败判断）
        import urllib.request
        urllib.request.urlopen(url + '/api', timeout=1.5)
        print(f'工作台已经在运行（{url}），直接打开浏览器。')
        if not a.no_browser:
            webbrowser.open(url)
        return
    except Exception:
        pass
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        srv = ThreadingHTTPServer(('127.0.0.1', a.port), H)
    except OSError:
        sys.exit(f'端口 {a.port} 被别的程序占用，可以加参数 --port 8766 换一个端口')
    print(f'Sysmac 迁移工作台已启动：{url}\n工作目录：{ROOT}\n接口清单：{url}/api\n关闭本窗口即停止。', flush=True)
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    srv.serve_forever()


if __name__ == '__main__':
    main()
