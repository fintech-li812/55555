import json,sys,threading,unittest
from pathlib import Path
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
import web_server as web
from http.server import HTTPServer
from program_engine import calculate_source

class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=HTTPServer(('127.0.0.1',0),web.Handler)
        cls.port=cls.server.server_port;cls.previous=web.PORT;web.PORT=cls.port
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();web.PORT=cls.previous
    def request(self,path,data=None,headers=None):
        req=Request(f'http://127.0.0.1:{self.port}'+path,
                    data=None if data is None else json.dumps(data).encode(),headers=headers or {})
        try:
            with urlopen(req,timeout=5) as r:return r.status,r.read(),dict(r.headers)
        except HTTPError as r:return r.code,r.read(),dict(r.headers)
    def auth(self):return {'Content-Type':'application/json','Origin':f'http://127.0.0.1:{self.port}','X-Workbench-Token':web.TOKEN}
    def test_html_real_page(self):
        status,body,headers=self.request('/')
        self.assertEqual(status,200);self.assertIn('运行计算'.encode(),body)
        self.assertNotIn(b'__CSRF_TOKEN__',body);self.assertEqual(headers['Cache-Control'],'no-store')
    def test_live_calculation(self):
        status,body,_=self.request('/api/calculate?source_id=program_demo_v1')
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['result']['amounts']['closing'],'140000.00')
    def test_source_switch_no_silent_demo_fallback(self):
        status,body,_=self.request('/api/calculate?source_id=manufacturing_sample_v1')
        self.assertEqual(status,200);self.assertIsNone(json.loads(body)['result'])
    def test_unknown_source(self):
        self.assertEqual(self.request('/api/calculate?source_id=../../secret')[0],422)
    def test_actual_original_audit_endpoint(self):
        status,body,_=self.request('/api/audit?source_id=manufacturing_sample_v1');out=json.loads(body)
        self.assertEqual(status,200);self.assertEqual(len(out['sheet_names']),8)
        self.assertEqual(out['normal_partial_receipt_rows'],[16,22,26]);self.assertFalse(out['full_data_gate_executed'])
    def test_audit_does_not_fallback_to_demo(self):
        self.assertEqual(self.request('/api/audit?source_id=program_demo_v1')[0],422)
    def test_external_host_denied(self):
        self.assertEqual(self.request('/api/sources',headers={'Host':'attacker.example'})[0],403)
    def test_write_requires_session(self):self.assertEqual(self.request('/api/save',{})[0],403)
    def test_write_requires_local_origin(self):
        headers=self.auth();headers['Origin']='https://attacker.example'
        self.assertEqual(self.request('/api/save',{},headers)[0],403)
    def test_tampered_result_no_database_access(self):
        result=calculate_source()['result'];result['source']['snapshot_ref']='fake'
        with patch('program_service.ProgramService.connect',side_effect=AssertionError('no_db_allowed')):
            status,body,_=self.request('/api/save',{'result':result,'request_id':'rejected'},self.auth())
        self.assertEqual(status,422);self.assertIn(b'result_differs_from_approved_snapshot',body)
    def test_unknown_save_fields(self):
        self.assertEqual(self.request('/api/save',{'skip_validation':True},self.auth())[0],422)
    def test_non_dict_save_result(self):
        self.assertEqual(self.request('/api/save',{'result':None,'request_id':'bad'},self.auth())[0],422)
    def test_no_static_file_path_access(self):self.assertEqual(self.request('/entry.py')[0],404)

if __name__=='__main__':unittest.main()
