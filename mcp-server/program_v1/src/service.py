"""Minimal v1: read one approved snapshot, explain, commit to MySQL, exact query.
No forecasts, no spreadsheet calculation and no synthesized production results.
"""
from datetime import datetime,timezone
import hashlib,json,re,uuid
from decimal import Decimal
import pymysql
from pymysql.cursors import DictCursor
from agent_guard import validate_result,render_result,CONTEXT

DDL='''CREATE TABLE mfg_cashflow_runs_v1 (
 record_id CHAR(36) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 tenant_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
 company_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
 operation VARCHAR(32) CHARACTER SET ascii NOT NULL,
 request_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
 period CHAR(7) CHARACTER SET ascii NOT NULL,
 source_revision VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
 run_id VARCHAR(128) COLLATE utf8mb4_bin NOT NULL,
 payload_sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 payload JSON NOT NULL,
 stored_at DATETIME(6) NOT NULL,
 UNIQUE KEY uq_request (tenant_id,company_id,operation,request_id),
 CHECK (payload_sha256 REGEXP '^[0-9a-f]{64}$')
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin'''

def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')

def strict_result(raw):
    def pairs(items):
        obj={}
        for key,value in items:
            if key in obj: raise ValueError('duplicate_json_key')
            obj[key]=value
        return obj
    def invalid(_):raise ValueError('nonfinite_json')
    obj=json.loads(raw,object_pairs_hook=pairs,parse_constant=invalid)
    canonical(obj)
    return obj

def binding(result):return {k:result[k] for k in CONTEXT}

class Service:
    def __init__(self,config):self.config=config.validate()
    def connect(self):
        c=self.config
        return pymysql.connect(host=c.host,port=c.port,user=c.user,password=c.password,database=c.database,
            charset='utf8mb4',cursorclass=DictCursor,autocommit=False,
            connect_timeout=5,read_timeout=8,write_timeout=8,
            init_command="SET time_zone='+00:00'")
    def validate(self,result):
        if not isinstance(result,dict):raise ValueError('result_object_required')
        if result.get('tenant_id')!=self.config.tenant or result.get('company_id')!=self.config.company:
            raise ValueError('trusted_identity_mismatch')
        for k in CONTEXT:
            if not isinstance(result.get(k),str) or len(result[k])>128:
                raise ValueError('context_length_invalid')
        canonical(result)
        validate_result(result,binding(result),allow_fixture=self.config.allow_fixture)
        if result['status']!='success':raise ValueError('only_success_is_formal_result')
        if result.get('origin')=='qwen_sheet' and not self.config.qwen_bound:
            raise ValueError('qwen_platform_binding_not_verified')
        return result
    def read(self):
        p=self.config.result_file
        if p.stat().st_size>500000:raise ValueError('snapshot_size_limit')
        raw=p.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=self.config.result_sha256:
            raise ValueError('snapshot_file_changed_reapprove_required')
        result=self.validate(strict_result(raw))
        return {'status':'ok','result':result,'explanation':render_result(result,binding(result),allow_fixture=self.config.allow_fixture),
                'source_file_sha256':self.config.result_sha256,'platform_native_verified':False,
                'fixture':result['origin']=='offline_fixture'}
    def _request(self,request_id):
        if not isinstance(request_id,str) or not request_id.strip() or len(request_id)>128:
            raise ValueError('request_id_invalid')
    def _receipt(self,row):
        return dict(schema_version='mfg-evidence.v1',kind='receipt',operation='save_cashflow_result',
            tenant_id=row['tenant_id'],company_id=row['company_id'],period=row['period'],
            source_revision=row['source_revision'],run_id=row['run_id'],request_id=row['request_id'],
            record_id=row['record_id'],payload_sha256=row['payload_sha256'],
            stored_at=row['stored_at'].replace(tzinfo=timezone.utc).isoformat(),status='committed')
    def _checked_row(self,row):
        result=self.validate(strict_result(row['payload']))
        if hashlib.sha256(canonical(result)).hexdigest()!=row['payload_sha256']:
            raise ValueError('stored_payload_digest_mismatch')
        if any(result[k]!=row[k] for k in CONTEXT):
            raise ValueError('stored_context_mismatch')
        return {'status':'committed','result':result,'receipt':self._receipt(row),
                'fixture':result['origin']=='offline_fixture'}
    def query(self,request_id,period,source_revision,run_id):
        self._request(request_id)
        if any(not isinstance(v,str) or not v or len(v)>128 for v in [period,source_revision,run_id]):
            raise ValueError('query_context_invalid')
        with self.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute('SELECT * FROM mfg_cashflow_runs_v1 WHERE tenant_id=%s AND company_id=%s AND operation=%s AND request_id=%s',
                    (self.config.tenant,self.config.company,'save_cashflow_result',request_id))
                row=cursor.fetchone()
        if not row:return {'status':'not_found'}
        if (row['period'],row['source_revision'],row['run_id'])!=(period,source_revision,run_id):
            return {'status':'conflict','reason':'request_exists_with_other_version'}
        return self._checked_row(row)
    def approved_result(self,result):
        return self.read()['result']
    def save(self,result,request_id):
        self._request(request_id)
        self.validate(result)
        # Agent cannot invent a new result: it must equal the approved current snapshot.
        approved=self.approved_result(result)
        if canonical(result)!=canonical(approved):raise ValueError('result_differs_from_approved_snapshot')
        digest=hashlib.sha256(canonical(result)).hexdigest()
        conn=None
        try:
            conn=self.connect()
            with conn.cursor() as cursor:
                try:
                    cursor.execute('INSERT INTO mfg_cashflow_runs_v1 '
                        '(record_id,tenant_id,company_id,operation,request_id,period,source_revision,run_id,payload_sha256,payload,stored_at) '
                        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,UTC_TIMESTAMP(6))',
                        (str(uuid.uuid4()),self.config.tenant,self.config.company,'save_cashflow_result',request_id,
                         result['period'],result['source_revision'],result['run_id'],digest,canonical(result).decode('utf-8')))
                except pymysql.err.IntegrityError as exc:
                    conn.rollback()
                    if exc.args[0]!=1062:raise
                    found=self.query(request_id,result['period'],result['source_revision'],result['run_id'])
                    if found['status']=='committed' and found['receipt']['payload_sha256']==digest:return found
                    return {'status':'conflict','reason':'same_request_other_payload'}
            conn.commit()  # No receipt before real commit.
        except (pymysql.err.OperationalError,pymysql.err.InterfaceError):
            return {'status':'pending','reason':'database_outcome_unconfirmed_query_same_request'}
        finally:
            if conn is not None and conn.open:
                try:conn.close()
                except (pymysql.err.OperationalError,pymysql.err.InterfaceError):pass
        try:
            return self.query(request_id,result['period'],result['source_revision'],result['run_id'])
        except (pymysql.err.OperationalError,pymysql.err.InterfaceError):
            return {'status':'pending','reason':'committed_response_unconfirmed_query_same_request'}
