"""Pinned, synthetic, fixed-template inputs; runtime arithmetic, never cached results."""
from pathlib import Path
from decimal import Decimal, localcontext
import hashlib, io, json, re, zipfile
from xml.etree import ElementTree as ET
from agent_guard import money

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'manufacturing-program-arithmetic.v1'
SOURCES = {'program_demo_v1': '制造业_程序计算合成输入.xlsx',
           'program_difference_v1': '制造业_余额差异合成输入.xlsx'}
NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
FIELDS = ('schema_version data_type tenant_id company_id period unit coverage account_scope '
          'opening_source closing_source operating_confirmed investing_confirmed financing_confirmed '
          'opening operating_inflow operating_outflow investing_inflow investing_outflow '
          'financing_inflow financing_outflow actual_closing').split()
MONEY_FIELDS = ['opening', 'actual_closing'] + [g+'_'+d for g in ('operating','investing','financing') for d in ('inflow','outflow')]

def canonical(data):
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')

def sha(raw): return hashlib.sha256(raw).hexdigest()

def read_template(raw):
    if len(raw) > 1_000_000: raise ValueError('input_workbook_size_limit')
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        infos=z.infolist()
        expected={'[Content_Types].xml','_rels/.rels','xl/workbook.xml',
                  'xl/_rels/workbook.xml.rels','xl/worksheets/sheet1.xml','xl/styles.xml'}
        if len(infos)!=6 or {i.filename for i in infos}!=expected or sum(i.file_size for i in infos)>2_000_000:
            raise ValueError('fixed_template_container_required')
        def xml(name):
            data=z.read(name)
            if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper(): raise ValueError('xml_entities_forbidden')
            return ET.fromstring(data)
        book=xml('xl/workbook.xml')
        if [s.get('name') for s in book.findall('s:sheets/s:sheet',NS)]!=['输入']:
            raise ValueError('fixed_input_sheet_required')
        cells={}
        for c in xml('xl/worksheets/sheet1.xml').findall('.//s:sheetData/s:row/s:c',NS):
            ref=c.get('r','')
            if ref in cells: raise ValueError('duplicate_cell')
            if not re.fullmatch(r'[ABC][1-9][0-9]*',ref): raise ValueError('unexpected_cell')
            if c.find('s:f',NS) is not None: raise ValueError('input_formulas_forbidden')
            if c.get('t')!='inlineStr': raise ValueError('fixed_template_text_cells_required')
            cells[ref]=''.join(n.text or '' for n in c.findall('.//s:t',NS))
        if set(cells)!={f'{col}{row}' for row in range(1,len(FIELDS)+2) for col in 'ABC'}:
            raise ValueError('missing_or_extra_template_cells')
        if [cells[c+'1'] for c in 'ABC']!=['字段','值','说明']: raise ValueError('input_headers_mismatch')
        result={}
        for i,key in enumerate(FIELDS,2):
            if cells['A'+str(i)]!=key: raise ValueError('input_field_order_mismatch')
            result[key]=cells['B'+str(i)]
    return result

def calculate_input(data, input_sha256, source_id='program_demo_v1'):
    if not isinstance(data,dict) or set(data)!=set(FIELDS): raise ValueError('input_fields_incomplete_or_unknown')
    if any(not isinstance(v,str) or not v.strip() or len(v)>128 for v in data.values()):
        raise ValueError('input_text_invalid')
    exact={'schema_version':'mfg-program-input.v1','data_type':'SYNTHETIC',
           'tenant_id':'fixture-tenant','company_id':'fixture-manufacturing',
           'unit':'CNY','coverage':'full_cashflow'}
    if any(data[k]!=v for k,v in exact.items()): raise ValueError('synthetic_scope_or_unit_invalid')
    if not re.fullmatch(r'20[0-9]{2}-(0[1-9]|1[0-2])',data['period']): raise ValueError('period_invalid')
    if not re.fullmatch(r'[0-9a-f]{64}',input_sha256): raise ValueError('input_digest_invalid')
    if data['opening_source']==data['closing_source']: raise ValueError('independent_balance_sources_required')
    if any(data[g+'_confirmed']!='true' for g in ('operating','investing','financing')):
        raise ValueError('three_cashflow_categories_must_be_explicitly_confirmed')
    with localcontext() as ctx:
        ctx.prec=50
        vals={k:money(data[k]) for k in MONEY_FIELDS}
        if any(vals[k]<0 for k in MONEY_FIELDS if k not in ('opening','actual_closing')):
            raise ValueError('gross_cashflows_must_be_nonnegative')
        inflow=sum((vals[g+'_inflow'] for g in ('operating','investing','financing')),Decimal('0.00'))
        outflow=sum((vals[g+'_outflow'] for g in ('operating','investing','financing')),Decimal('0.00'))
        net=inflow-outflow; closing=vals['opening']+net; difference=vals['actual_closing']-closing
        amounts=dict(opening=vals['opening'],inflow=inflow,outflow=outflow,net=net,closing=closing,
                     actual_closing=vals['actual_closing'],difference=difference)
        amounts={k:format(v,'.2f') for k,v in amounts.items()}
        for v in amounts.values(): money(v)
    algorithm=sha(Path(__file__).read_bytes())
    revision='program-'+input_sha256[:20]
    run='calc-'+sha(canonical({'input':input_sha256,'algorithm':algorithm,'source_id':source_id}))[:32]
    result={k:data[k] for k in ('tenant_id','company_id','period')}
    result.update(source_revision=revision,run_id=run,schema_version='mfg-agent-result.v1',
        industry='manufacturing',status='success',origin='program_calculation',data_type='SYNTHETIC',
        unit='CNY',coverage='full_cashflow',calculation_version=VERSION,issues=[],amounts=amounts,
        source={'workbook_ref':source_id,'sheet':'输入','range':f'A1:C{len(FIELDS)+1}','snapshot_ref':input_sha256},
        execution={'input_sha256':input_sha256,'algorithm_sha256':algorithm,'engine':'Python Decimal',
                   'native_qwen_sheet':False,'source_modified':False},
        evidence={'account_scope':data['account_scope'],'opening_source':data['opening_source'],
                  'closing_source':data['closing_source'],'source_assurance':'synthetic_declared_not_audited'},
        reconciliation_state='matched' if difference==0 else 'difference_pending_review',
        risk_flags=[name for name,trigger in [('negative_closing_cash',closing<0),
                                            ('independent_balance_difference',difference!=0)] if trigger])
    return result

def registry():
    data=json.loads((ROOT/'inputs/registry.json').read_text(encoding='utf-8'))
    if set(data)!=set(SOURCES): raise ValueError('source_registry_invalid')
    return data

def calculate_source(source_id='program_demo_v1'):
    if source_id not in SOURCES: raise ValueError('source_id_not_registered_for_program_calculation')
    p=ROOT/'inputs'/SOURCES[source_id]
    if p.is_symlink() or p.stat().st_size>1_000_000: raise ValueError('source_path_or_size_invalid')
    raw=p.read_bytes(); digest=sha(raw)
    if digest!=registry()[source_id]['sha256']: raise ValueError('source_changed_requires_explicit_rebinding')
    result=calculate_input(read_template(raw),digest,source_id)
    if p.read_bytes()!=raw: raise ValueError('source_changed_during_calculation')
    return {'status':'calculated','result':result,'fixture':True,'platform_native_verified':False,
            'can_save_source_result':result['reconciliation_state']=='matched',
            'source_file_sha256':digest,'database_written':False}

def catalog():
    records=[]
    for sid,name in SOURCES.items():
        try:
            calculate_source(sid); status='available'
        except (OSError,ValueError,zipfile.BadZipFile,ET.ParseError): status='unavailable'
        records.append({'source_id':sid,'logical_name':name,'kind':'synthetic_program_input',
                        'status':status,'native_qwen_workbook':False})
    return records
