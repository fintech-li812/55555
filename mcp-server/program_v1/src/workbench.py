"""Bound project inspection. No arbitrary paths, workbook writes, or financial inference."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib,io,json,os,posixpath,re,zipfile
from xml.etree import ElementTree as ET
from config import Config
from service import Service

ROOT=Path(__file__).resolve().parents[1]
# The sample workbook lives at <project>/data/. It used to be pinned to a
# developer-machine absolute path (D:\CashflowRiskWorkbench\...), which can
# never resolve on the Linux server -- so the controlled sample was always
# reported as "unavailable" and the eight-sheet audit never ran in production.
# Resolve it relative to this file instead, and allow an explicit override for
# deployments that keep the workbook elsewhere.
SOURCE_PATH=Path(os.environ.get('WORKBENCH_SAMPLE_XLSX')
                 or (ROOT.parents[1]/'data'/'制造业_样例数据.xlsx'))
SOURCE_SHA256='7e32b1eca8cfe892bd0ee96dfe257b03965018e607fc0e8cbaface33c13d0a3b'
SOURCE_ID='manufacturing_sample_v1'
NS={'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
REL_ID='{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'

def _source_bytes(source_id):
    if source_id!=SOURCE_ID:raise ValueError('source_id_not_registered')
    if not SOURCE_PATH.is_file() or SOURCE_PATH.is_symlink():raise ValueError('source_unavailable')
    if SOURCE_PATH.stat().st_size>10_000_000:raise ValueError('source_size_limit')
    raw=SOURCE_PATH.read_bytes()
    digest=hashlib.sha256(raw).hexdigest()
    if digest!=SOURCE_SHA256:raise ValueError('source_changed_requires_rebinding')
    return digest,raw

def _bound_source(source_id):return _source_bytes(source_id)[0]

def list_sources():
    try:
        digest=_bound_source(SOURCE_ID);status='available'
    except (ValueError,OSError) as exc:
        digest=None;status='unavailable'
    return {'schema_version':'mfg-source-catalog.v1','sources':[{'source_id':SOURCE_ID,
        'logical_name':'制造业_样例数据.xlsx','kind':'controlled_sample','status':status,
        'expected_sha256':SOURCE_SHA256,'actual_sha256':digest,'native_qwen_workbook':False}],
        'other_industries_enabled':False}

def _xml(archive,name):
    raw=archive.read(name)
    if len(raw)>8_000_000 or b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
        raise ValueError('unsupported_or_oversized_xml')
    return ET.fromstring(raw)

def _cell(node,strings):
    typ=node.get('t','n');v=node.find('s:v',NS);f=node.find('s:f',NS)
    raw=None if v is None else v.text
    if typ=='inlineStr':value=''.join(t.text or '' for t in node.findall('.//s:is//s:t',NS))
    elif typ=='s' and raw is not None:
        index=int(raw)
        if index<0 or index>=len(strings):raise ValueError('shared_string_index_invalid')
        value=strings[index]
    else:value=raw
    if value is not None and len(value)>50_000:raise ValueError('cell_text_limit')
    return {'value':value,'cell_type':typ,'raw_xml_value':raw,'formula':None if f is None else f.text,
        'has_formula':f is not None,'has_cached_value':v is not None and raw is not None}

def inspect_source(source_id=SOURCE_ID):
    digest,raw=_source_bytes(source_id)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        infos=archive.infolist();names=[i.filename for i in infos]
        if len(infos)>1000 or len(set(names))!=len(names) or sum(i.file_size for i in infos)>32_000_000:
            raise ValueError('workbook_container_limit')
        workbook=_xml(archive,'xl/workbook.xml');rels=_xml(archive,'xl/_rels/workbook.xml.rels')
        targets={}
        for rel in rels:
            if rel.get('TargetMode')=='External':continue
            raw=rel.get('Target','')
            targets[rel.get('Id')]=posixpath.normpath(raw.lstrip('/') if raw.startswith('/') else posixpath.join('xl',raw))
        sheet_nodes=workbook.findall('s:sheets/s:sheet',NS)
        sheets=[s.get('name') for s in sheet_nodes]
        if len(sheets)!=len(set(sheets)):raise ValueError('duplicate_sheet_names')
        strings=[]
        if 'xl/sharedStrings.xml' in names:
            strings=[''.join(t.text or '' for t in si.findall('.//s:t',NS)) for si in _xml(archive,'xl/sharedStrings.xml')]
        blocks={}
        for title,max_col,max_row in [('企业基本信息','C',10),('现金流水','I',15)]:
            selected=[s for s in sheet_nodes if s.get('name')==title]
            if len(selected)!=1:raise ValueError('required_sheet_missing')
            target=targets.get(selected[0].get(REL_ID),'')
            if not re.fullmatch(r'xl/worksheets/[^/]+\.xml',target):raise ValueError('sheet_relation_not_internal')
            cells={}
            for node in _xml(archive,target).findall('.//s:sheetData/s:row/s:c',NS):
                address=node.get('r','');match=re.fullmatch(r'([A-Z]+)([1-9][0-9]*)',address)
                if not match:raise ValueError('invalid_cell_address')
                if len(match[1])==1 and match[1]<=max_col and int(match[2])<=max_row:
                    if address in cells:raise ValueError('duplicate_cell_address')
                    cells[address]=_cell(node,strings)
            blocks[title]={'range':f'A1:{max_col}{max_row}','cells':cells}
        props=workbook.find('s:workbookPr',NS)
        date_system='1904' if props is not None and props.get('date1904') in ('1','true') else '1900'
    # Recheck after parsing: do not attach an earlier digest to a later read.
    if _bound_source(source_id)!=digest:raise ValueError('source_changed_during_read')
    return {'schema_version':'mfg-source-inspection.v1','status':'inspected','source_id':source_id,
        'source_kind':'controlled_sample','source_file_sha256':digest,'sheet_names':sheets,
        'date_system':date_system,'read_at':datetime.now(timezone.utc).isoformat(),'blocks':blocks,
        'read_method':'local OOXML read-only; not Qwen native sheet API',
        'money_conversion_performed':False,'formula_execution_performed':False,'source_modified':False}

def readiness(source_id=SOURCE_ID):
    inspected=inspect_source(source_id);cells=inspected['blocks']['现金流水']['cells']
    at=lambda a:cells.get(a,{}).get('value')
    problems=[]
    if at('D8')=='1631300':problems.append({'code':'unit_confirmation_required','source':'现金流水!D8',
        'observed':'1631300','detail':'已知单位疑点，保留原值，不自动缩放或放大。'})
    a=[at(f'{c}12') for c in 'ABCDEFGHI'];b=[at(f'{c}15') for c in 'ABCDEFGHI']
    if a==b and any(v is not None for v in a):problems.append({'code':'duplicate_rows_preserved',
        'source':'现金流水!A12:I12 / A15:I15','detail':'两行原值相同；不自行删除或合并。'})
    for code,detail in [
        ('native_recalculation_not_bound','未登记千问原生工作簿及本次重算证据；本地固定值不是原生公式执行结果。'),
        ('full_cashflow_mapping_incomplete','当前只绑定经营现金流水取证；投资及筹资未接入完整映射，不假定为0。'),
        ('account_scope_missing','尚未绑定统计账户范围。'),
        ('opening_balance_not_bound','尚未绑定用于本次全口径核对的期初余额来源。'),
        ('independent_closing_not_bound','尚未绑定独立实际期末余额，不能自己与计算期末对平。')]:
        problems.append({'code':code,'detail':detail})
    return {'schema_version':'mfg-workbench-readiness.v1','status':'blocked','source_id':source_id,
        'source_kind':'controlled_sample','source_file_sha256':inspected['source_file_sha256'],
        'can_claim_full_cashflow':False,'can_save_source_result':False,'issues':problems,
        'gate_rules_executed':False,'normal_partial_receipts_treated_as_errors':False,
        'note':'工程演示的offline_fixture结果属于另一来源，不能拿来填补这个源文件的缺项。'}

def project_status():
    service=Service(Config.from_env());loaded=service.read();database='unavailable'
    try:
        with service.connect() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) n FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='mfg_cashflow_runs_v1'")
                database='connected' if cur.fetchone()['n']==1 else 'table_missing'
    except Exception:pass
    return {'schema_version':'mfg-workbench-status.v1','integration_version':'workbench-integration.v1',
        'mode':'manufacturing_qa','components':{
            'mcp_stdio_transport':'implemented','read_save_exact_query':'implemented','database':database,
            'bound_manufacturing_source':list_sources()['sources'][0]['status'],
            'fixture_baseline_arithmetic':'implemented_fixture_only','result_guard':'implemented',
            'excel_import_package':'awaiting_external_delivery','report_exporter':'awaiting_external_delivery',
            'native_sheet_recalculation':'not_bound','rolling_and_advanced_three_state':'phase_two'},
        'result_profile':{'origin':loaded['result']['origin'],'fixture':loaded['fixture'],
            'company_id':loaded['result']['company_id'],'period':loaded['result']['period'],
            'source_revision':loaded['result']['source_revision']},
        'production_deployed':False,'source_data_read_only':True,
        'financial_result_source_is_separate_from_inspected_workbook':True}
