"""Real MCP and real QA SELECT verification. Never invokes a successful save."""
from pathlib import Path
from datetime import timedelta
import asyncio,json,os,sys
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))
import entry
os.environ.update(entry.environment())
from config import Config
from program_service import ProgramService
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client

def count_rows():
    with ProgramService(Config.from_env()).connect() as conn:
        with conn.cursor() as cur:
            cur.execute('START TRANSACTION READ ONLY')
            cur.execute('SELECT COUNT(*) n FROM mfg_cashflow_runs_v1');n=cur.fetchone()['n']
        conn.rollback()
    return n

async def main():
    before=count_rows();calls=[]
    params=StdioServerParameters(command=sys.executable,args=['-B','-u',str(ROOT/'entry.py'),'stdio'],env=entry.environment())
    with (ROOT/'evidence/runtime_mcp_stderr_v1_1.txt').open('x',encoding='utf-8') as log:
        async with stdio_client(params,errlog=log) as (read,write):
            async with ClientSession(read,write,read_timeout_seconds=timedelta(seconds=30)) as session:
                await session.initialize();names=sorted(t.name for t in (await session.list_tools()).tools)
                assert len(names)==9 and 'calculate_manufacturing_cashflow' in names
                async def call(name,args):
                    response=await session.call_tool(name,args)
                    assert not response.isError
                    data=response.structuredContent if response.structuredContent is not None else json.loads(response.content[0].text)
                    calls.append({'tool':name,'arguments':args,'result':data});return data
                status=await call('get_workbench_status',{});assert status['components']['database']=='connected'
                assert status['architecture']=='qwen_entry_program_calculation'
                sources=await call('list_manufacturing_sources',{});assert len(sources['sources'])==3
                result=await call('calculate_manufacturing_cashflow',{'source_id':'program_demo_v1'})
                assert result['result']['amounts']['closing']=='140000.00'
                assert result['result']['origin']=='program_calculation'
                loaded=await call('read_cashflow_result',{});assert loaded['result']==result['result']
                bad=await call('calculate_manufacturing_cashflow',{'source_id':'program_difference_v1'})
                assert bad['can_save_source_result'] is False and bad['result']['amounts']['difference']=='-1000.00'
                blocked=await call('calculate_manufacturing_cashflow',{'source_id':'manufacturing_sample_v1'})
                assert blocked['status']=='blocked' and blocked['result'] is None
                history=await call('query_cashflow_result',dict(request_id='qwen-v2-native-20261024-01',period='2026-08',source_revision='fixture-source-v1',run_id='fixture-run-202608'))
                assert history['receipt']['record_id']=='d7f97377-2fd2-45ff-8046-49297bf15c61'
    after=count_rows();assert before==after
    report={'status':'passed','tools':names,'successful_calls':len(calls),'qa_rows_before':before,'qa_rows_after':after,
            'database_writes_performed':False,'qwen_native_call':False,'calls':calls}
    with (ROOT/'evidence/runtime_mcp_and_sql_v1_1.json').open('x',encoding='utf-8') as f:json.dump(report,f,ensure_ascii=False,indent=2)
    print(json.dumps({k:v for k,v in report.items() if k!='calls'},ensure_ascii=False))

if __name__=='__main__':asyncio.run(main())
