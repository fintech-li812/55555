from pathlib import Path
from datetime import timedelta
import asyncio,json,os,sys
ROOT=Path(__file__).resolve().parent
OUT=Path('D:/AI_Command_Workspace/交付/CX-ORIGINAL-SOURCE-001/Codex/v1/evidence')
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))
import entry
os.environ.update(entry.environment())
from config import Config
from program_service import ProgramService
from service import canonical
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

def count_rows():
    with ProgramService(Config.from_env()).connect() as conn:
        with conn.cursor() as cur:
            cur.execute('START TRANSACTION READ ONLY');cur.execute('SELECT COUNT(*) n FROM mfg_cashflow_runs_v1');n=cur.fetchone()['n']
        conn.rollback()
    return n

async def main():
    before=count_rows();calls=[]
    params=StdioServerParameters(command=sys.executable,args=['-B','-u',str(ROOT/'entry.py'),'stdio'],env=entry.environment())
    with (OUT/'mcp_original_source_stderr.txt').open('x',encoding='utf-8') as log:
        async with stdio_client(params,errlog=log) as (read,write):
            async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=30)) as s:
                await s.initialize();names=sorted(t.name for t in (await s.list_tools()).tools)
                assert len(names)==9 and 'get_manufacturing_data_audit' in names
                async def call(name,args):
                    r=await s.call_tool(name,args);assert not r.isError
                    data=r.structuredContent if r.structuredContent is not None else json.loads(r.content[0].text)
                    calls.append({'tool':name,'arguments':args,'result':data});return data
                status=await call('get_workbench_status',{});assert status['integration_version']=='workbench-program.v1.1'
                inspected=await call('inspect_manufacturing_source',{'source_id':'manufacturing_sample_v1'})
                assert len(inspected['blocks'])==8 and inspected['blocks']['现金流水']['cells']['D8']['value']=='1631300'
                audit=await call('get_manufacturing_data_audit',{'source_id':'manufacturing_sample_v1'})
                assert len(audit['issues'])==5 and audit['normal_partial_receipt_rows']==[16,22,26]
                assert len(audit['mapping_preview'])==46 and audit['full_data_gate_executed'] is False
                ready=await call('get_manufacturing_readiness',{'source_id':'manufacturing_sample_v1'})
                assert ready['gate_rules_executed'] and ready['status']=='blocked' and ready['account_binding']['module_connected']
                assert ready['cash_selection']['module_connected'] and ready['can_save_source_result'] is False
                normal=await call('calculate_manufacturing_cashflow',{'source_id':'program_demo_v1'})
                assert normal['result']['amounts']['closing']=='140000.00'
                difference=await call('calculate_manufacturing_cashflow',{'source_id':'program_difference_v1'})
                assert difference['can_save_source_result'] is False
                r=normal['result']
                stored=await call('query_cashflow_result',dict(request_id='qwen-program-20261026-first-01',period=r['period'],source_revision=r['source_revision'],run_id=r['run_id']))
                assert canonical(stored['result'])==canonical(r)
    after=count_rows();assert before==after
    report=dict(status='passed',tools=names,readonly_successful_calls=len(calls),qa_rows_before=before,qa_rows_after=after,
        qwen_current_process_refreshed=False,database_writes_performed=False,calls=calls)
    with (OUT/'mcp_original_source_sql_verification.json').open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in report.items() if k!='calls'},ensure_ascii=False))

if __name__=='__main__':asyncio.run(main())
