"""Reuse existing QA persistence; approved result is recomputed, never Agent-authored."""
from service import Service, canonical, binding
from agent_guard import render_result
from program_engine import calculate_source
import eight_sheet_engine as eight

class ProgramService(Service):
    def read_source(self,source_id='program_demo_v1'):
        out=eight.calculate() if source_id==eight.SOURCE_ID else calculate_source(source_id)
        self.validate(out['result'])
        out.update(status='ok',explanation=render_result(out['result'],binding(out['result']),allow_fixture=True))
        return out

    def read(self):
        return self.read_source()

    def approved_result(self,result):
        sid=result.get('source',{}).get('workbook_ref')
        if sid not in ('program_demo_v1',eight.SOURCE_ID):raise ValueError('source_not_approved_for_save')
        out=self.read_source(sid)
        if not out.get('can_save_source_result'):raise ValueError('source_not_ready_for_save')
        return out['result']

    def save(self,result,request_id):
        if not isinstance(result,dict):raise ValueError('result_object_required')
        if result.get('origin')=='program_calculation' and result.get('reconciliation_state')!='matched':
            raise ValueError('independent_balance_difference_blocks_save')
        out=super().save(result,request_id)
        if out.get('result',{}).get('origin')=='program_calculation':out['fixture']=True
        return out

    def query(self,*args,**kwargs):
        out=super().query(*args,**kwargs)
        if out.get('result',{}).get('origin')=='program_calculation':out['fixture']=True
        return out
