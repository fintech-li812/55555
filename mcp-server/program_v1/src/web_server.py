"""Loopback companion UI. Qwen remains the natural-language entry; no automatic writes."""
from http.server import HTTPServer,BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse,parse_qs
import json,secrets
from config import Config
from program_service import ProgramService
from program_workbench import project_status,list_sources,calculate,readiness,data_audit
from service import strict_result
from source_audit import audit_source

PORT=18766
WEB=Path('D:/CashflowRiskWorkbench/web/工作台_程序计算版.html')
TOKEN=secrets.token_urlsafe(32)

class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(8)
    def log_message(self,*args):pass
    def respond(self,data,code=200,typ='application/json; charset=utf-8'):
        raw=data.encode('utf-8') if isinstance(data,str) else json.dumps(data,ensure_ascii=False,allow_nan=False).encode('utf-8')
        self.send_response(code)
        for k,v in {'Content-Type':typ,'Content-Length':str(len(raw)),
                    'Cache-Control':'no-store','X-Content-Type-Options':'nosniff',
                    'Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"}.items():self.send_header(k,v)
        self.end_headers();self.wfile.write(raw)
    def trusted(self):
        return self.headers.get('Host') in {f'127.0.0.1:{PORT}',f'localhost:{PORT}'}
    def do_GET(self):
        if not self.trusted():return self.respond({'error':'local_host_required'},403)
        path=urlparse(self.path);params=parse_qs(path.query)
        try:
            if path.path=='/':return self.respond(WEB.read_text(encoding='utf-8').replace('__CSRF_TOKEN__',TOKEN),typ='text/html; charset=utf-8')
            if path.path=='/api/status':return self.respond(project_status())
            if path.path=='/api/sources':return self.respond(list_sources())
            if path.path=='/api/calculate':return self.respond(calculate(params.get('source_id',['program_demo_v1'])[0]))
            if path.path=='/api/readiness':return self.respond(readiness(params.get('source_id',['manufacturing_sample_v1'])[0]))
            if path.path=='/api/audit':return self.respond(data_audit(params.get('source_id',['manufacturing_sample_v1'])[0]))
            return self.respond({'error':'not_found'},404)
        except (ValueError,OSError) as exc:return self.respond({'error':type(exc).__name__,'detail':str(exc)},422)
        except Exception:return self.respond({'error':'operation_failed','detail':'操作失败；未确认数据库提交时请使用同一请求编号回查。'},503)
    def do_POST(self):
        # Drain the bounded body before rejecting: closing with unread POST bytes
        # can reset the socket on Windows and hide the intended error response.
        try:
            size=int(self.headers.get('Content-Length','0'))
            if size<1 or size>512000:return self.respond({'error':'request_size_invalid'},413)
            raw=self.rfile.read(size)
            if len(raw)!=size:return self.respond({'error':'incomplete_request_body'},400)
        except (ValueError,TimeoutError):return self.respond({'error':'request_body_invalid'},400)
        if not self.trusted() or self.headers.get('Origin') not in {f'http://127.0.0.1:{PORT}',f'http://localhost:{PORT}'} or not secrets.compare_digest(self.headers.get('X-Workbench-Token',''),TOKEN):
            return self.respond({'error':'local_session_required'},403)
        try:
            if self.headers.get('Content-Type','').split(';')[0]!='application/json':return self.respond({'error':'json_required'},415)
            data=strict_result(raw);service=ProgramService(Config.from_env())
            if urlparse(self.path).path=='/api/save':
                if not isinstance(data,dict) or set(data)!={'result','request_id'}:raise ValueError('save_fields_invalid')
                return self.respond(service.save(data['result'],data['request_id']))
            if urlparse(self.path).path=='/api/query':
                if not isinstance(data,dict) or set(data)!={'request_id','period','source_revision','run_id'}:raise ValueError('query_fields_invalid')
                return self.respond(service.query(**data))
            return self.respond({'error':'not_found'},404)
        except (ValueError,TypeError,OSError) as exc:return self.respond({'error':type(exc).__name__,'detail':str(exc)},422)
        except Exception:return self.respond({'error':'operation_failed','detail':'操作失败；请使用同一请求编号回查，不要更换编号重复保存。'},503)

def main():
    server=HTTPServer(('127.0.0.1',PORT),Handler)
    print(f'Companion UI: http://127.0.0.1:{PORT}; QA only, no automatic write',flush=True)
    server.serve_forever()
