"""Original seven tools plus explicit runtime calculation, stdio only."""
from mcp.server.fastmcp import FastMCP
from config import Config
from program_service import ProgramService
from program_workbench import project_status,list_sources,inspect_source,readiness,calculate,data_audit

def build_mcp():
    server=FastMCP('manufacturing-workbench-program-v1')
    def service():return ProgramService(Config.from_env())
    @server.tool()
    def get_workbench_status()->dict:
        """Actual program architecture, source profiles and database connectivity."""
        return project_status()
    @server.tool()
    def list_manufacturing_sources()->dict:
        """List pinned original sample and distinct synthetic runtime calculation inputs."""
        return list_sources()
    @server.tool()
    def inspect_manufacturing_source(source_id:str='manufacturing_sample_v1')->dict:
        """Read-only source inspection; never modify original data or imply native sheet execution."""
        return inspect_source(source_id)
    @server.tool()
    def get_manufacturing_readiness(source_id:str='manufacturing_sample_v1')->dict:
        """Validate input readiness; source issues remain blocking under program architecture."""
        return readiness(source_id)
    @server.tool()
    def get_manufacturing_data_audit(source_id:str='manufacturing_sample_v1')->dict:
        """Audit registered original or complete synthetic eight-sheet source. Original remains blocked; synthetic is labelled. No writes."""
        return data_audit(source_id)
    @server.tool()
    def calculate_manufacturing_cashflow(source_id:str='program_demo_v1')->dict:
        """Actually read pinned workbook and calculate with Decimal; SYNTHETIC only; no database write."""
        return calculate(source_id)
    @server.tool()
    def read_cashflow_result(source_id:str='program_demo_v1')->dict:
        """Recompute current pinned synthetic workbook; returns source and algorithm digests, not static snapshot."""
        return service().read_source(source_id)
    @server.tool()
    def save_cashflow_result(result:dict,request_id:str)->dict:
        """Save only an exact recomputation of current approved synthetic source to isolated QA MySQL."""
        return service().save(result,request_id)
    @server.tool()
    def query_cashflow_result(request_id:str,period:str,source_revision:str,run_id:str)->dict:
        """Exact query within trusted QA tenant/company; compatible with historical fixture records."""
        return service().query(request_id,period,source_revision,run_id)
    return server

if __name__=='__main__':build_mcp().run(transport='stdio')
