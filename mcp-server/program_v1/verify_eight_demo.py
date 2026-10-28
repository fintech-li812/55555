from pathlib import Path
from datetime import timedelta
import asyncio,json,os,sys
ROOT=Path(__file__).resolve().parent
OUT=Path('D:/AI_Command_Workspace/交付/CX-EIGHT-DEMO-001/Codex/v1/evidence')
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))
import entry
os.environ.update(entry.environment())
from config import Config
from program_service import ProgramService
from service import canonical
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

def rows():
    with ProgramService(Config.from_env()).connect() as conn:
        with conn.cursor() as cur:
            cur.execute('START TRANSACTION READ ONLY');cur.execute('SELECT COUNT(*) n FROM mfg_cashflow_runs_v1');n=cur.fetchone()['n']
        conn.rollback()
    return n

async def main():
    before=rows();calls=[]
    params=StdioServerParameters(command=sys.executable,args=['-B','-u',str(ROOT/'entry.py'),'stdio'],env=entry.environment())
    with (OUT/'complete_eight_mcp_stderr.txt').open('x',encoding='utf-8') as log:
        async with stdio_client(params,errlog=log) as (read,write):
            async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=30)) as session:
                await session.initialize();tools=sorted(t.name for t in (await session.list_tools()).tools);assert len(tools)==9
                async def call(name,args):
                    out=await session.call_tool(name,args);assert not out.isError
                    data=out.structuredContent if out.structuredContent is not None else json.loads(out.content[0].text)
                    calls.append({'tool':name,'arguments':args,'result':data});return data
                status=await call('get_workbench_status',{});assert status['integration_version']=='workbench-program.v1.2'
                catalog=await call('list_manufacturing_sources',{});assert len(catalog['sources'])==4
                sid='synthetic_eight_sheet_v1'
                inspect=await call('inspect_manufacturing_source',{'source_id':sid});assert len(inspect['blocks'])==8
                ready=await call('get_manufacturing_readiness',{'source_id':sid});assert ready['status']=='ready' and ready['data_type']=='SYNTHETIC'
                calculated=await call('calculate_manufacturing_cashflow',{'source_id':sid})
                assert calculated['result']['amounts']['closing']=='177000.00' and calculated['can_save_source_result']
                loaded=await call('read_cashflow_result',{'source_id':sid});assert canonical(loaded['result'])==canonical(calculated['result'])
                original=await call('calculate_manufacturing_cashflow',{'source_id':'manufacturing_sample_v1'});assert original['status']=='blocked'
                history=await call('query_cashflow_result',dict(request_id='qwen-program-20261026-first-01',period='2026-08',source_revision='program-00613baa679620b938c0',run_id='calc-59c95872d9bdca4dba547fd42d323f42'))
                assert history['receipt']['record_id']=='5ad6af0b-d85a-448f-808f-c18aad1e9eda'
                assert history['result']['amounts']['closing']=='140000.00'
    after=rows();assert before==after
    report=dict(status='passed',tools=tools,readonly_successful_calls=len(calls),qa_rows_before=before,qa_rows_after=after,
                successful_new_source_save_verified=False,database_writes_performed=False,qwen_new_source_called=False,calls=calls)
    with (OUT/'完整八表_MCP与只读数据库核验.json').open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in report.items() if k!='calls'},ensure_ascii=False))

if __name__=='__main__':asyncio.run(main())
