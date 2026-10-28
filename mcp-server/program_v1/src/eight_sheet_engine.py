"""Complete registered SYNTHETIC eight-sheet demonstration, with source-bound evidence."""
from pathlib import Path
from decimal import Decimal,localcontext
import hashlib,json
import account_binding as binding
import cash_selection
from source_audit import read_workbook,raw_value,data_rows,yuan,parsed_date,SHAPES
from program_engine import canonical,calculate_input

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'demo_eight'
SOURCE_ID='synthetic_eight_sheet_v1'
NAME='制造业_完整八表_合成演示_v1.xlsx'
WORKBOOK=ROOT.parents[1]/'data'/NAME
SHAPE={**SHAPES,'银行流水':12,'筹资信息':9,'投资现金流':8}
BANK_HEADERS=['收付日期','对手方','金额（万元）','用途','关联单据号','证据性质','方向','账户编号','现金流类别','流水编号','来源角色','是否内部转账']
FIN_HEADERS=['融资类型','机构','金额/额度（万元）','条件说明','到期日','实际收付日期','方向','账户编号','关联流水号']
INV_HEADERS=['项目','类型','发生/计划日期','金额（万元，支出为负）','状态','备注','账户编号','关联流水号']
VERSION='manufacturing-eight-sheet-program.v1'
def sha(raw):return hashlib.sha256(raw).hexdigest()
def val(book,sheet,c,r):return raw_value(book,sheet,f'{c}{r}')
def need(ok,reason):
    if not ok:raise ValueError(reason)

def derive_events(book,workbook_sha,context):
    """Map raw workbook cells, not cached normalized totals. Money converts once."""
    for sheet,headers in [('银行流水',BANK_HEADERS),('筹资信息',FIN_HEADERS),('投资现金流',INV_HEADERS)]:
        need([val(book,sheet,chr(65+i),2) for i in range(len(headers))]==headers,'eight_sheet_header_mismatch:'+sheet)
    need('SYNTHETIC' in str(val(book,'企业基本信息','B',27)),'synthetic_label_required')
    need(val(book,'企业基本信息','B',5)=='制造业','manufacturing_scope_required')
    for sheet,cols in [('现金流水',('D','E','F','G')),('应收账款',('E','F'))]:
        for col in cols:need('万元' in str(val(book,sheet,col,2)),'explicit_wanyuan_header_required')
    records=[];bank={};checks=[];observations=[]
    def record(sheet,row,kind,sid,**fields):
        key=sha(canonical({'workbook':workbook_sha,'sheet':sheet,'row':row,'kind':kind}))
        item=dict(record_id=f'{sheet}-{row}',source_record_key=key,kind=kind,source_id=sid,source_sheet=sheet,source_row=row,**fields)
        records.append(item);return item['record_id']
    def dt(sheet,col,r):return parsed_date(book,sheet,f'{col}{r}').isoformat()
    for r in data_rows(book,'银行流水'):
        txn=val(book,'银行流水','J',r);need(isinstance(txn,str) and txn and txn not in bank,'duplicate_or_missing_bank_transaction')
        direction=val(book,'银行流水','G',r);need(direction in ('inflow','outflow'),'bank_direction_required')
        amount=yuan(book,'银行流水',f'C{r}');need(amount>=0,'negative_bank_gross_amount')
        need(val(book,'银行流水','K',r)=='primary_actual','bank_primary_role_required')
        need(val(book,'银行流水','L',r)=='false','demo_internal_transfer_not_registered')
        need(val(book,'银行流水','I',r) in ('operating','investing','financing'),'bank_cash_category_required')
        rid=record('银行流水',r,'cash_in' if direction=='inflow' else 'cash_out','bank-primary',
            settlement_date=dt('银行流水','A',r),amount=format(amount,'.2f'),unit='CNY',
            account_id=val(book,'银行流水','H',r),category=val(book,'银行流水','I',r),transaction_id=txn)
        bank[txn]=next(x for x in records if x['record_id']==rid)
    links=[]
    for sheet,headers,category in [('投资现金流',INV_HEADERS,'investing'),('筹资信息',FIN_HEADERS,'financing')]:
        for r in data_rows(book,sheet):
            if sheet=='筹资信息':
                typ=val(book,sheet,'A',r);amount=yuan(book,sheet,f'C{r}');need(amount>=0,'negative_financing_value')
                if typ in ('银行授信额度','贷款余额'):
                    need(all(val(book,sheet,c,r) in (None,'') for c in 'FGHI'),'noncash_financing_has_cash_fields')
                    record(sheet,r,'credit_limit' if typ=='银行授信额度' else 'noncash_adjustment','business-docs',amount=format(amount,'.2f'),unit='CNY')
                    observations.append({'code':'noncash_financing_excluded','source':f'{sheet}!A{r}:I{r}','detail':typ+'不重复计入现金。'})
                    continue
                need(typ in ('实际借款收款','实际偿还本金'),'unsupported_financing_cash_type')
                direction=val(book,sheet,'G',r);expected='inflow' if typ=='实际借款收款' else 'outflow'
                need(direction==expected,'financing_direction_mismatch')
                date_=dt(sheet,'F',r);account=val(book,sheet,'H',r);txn=val(book,sheet,'I',r)
            else:
                signed=yuan(book,sheet,f'D{r}');amount=abs(signed)
                direction='outflow' if signed<0 else 'inflow';status=val(book,sheet,'E',r)
                need(status==('已支付' if direction=='outflow' else '已收款'),'investment_settlement_not_explicit')
                date_=dt(sheet,'C',r);account=val(book,sheet,'G',r);txn=val(book,sheet,'H',r)
            need(txn in bank,'business_cash_missing_bank_reference')
            b=bank[txn]
            need((b['category'],b['kind'],b['settlement_date'],b['amount'],b['account_id'])==
                 (category,'cash_in' if direction=='inflow' else 'cash_out',date_,format(amount,'.2f'),account),
                 'business_bank_source_values_mismatch')
            rid=record(sheet,r,b['kind'],'business-corroboration',settlement_date=date_,amount=format(amount,'.2f'),unit='CNY',account_id=account,category=category)
            links.append({'primary_record_id':b['record_id'],'corroborating_record_ids':[rid],
                'link_type':'transaction_evidence','evidence_ref':'SYNTHETIC-EXACT-WORKBOOK-LINK'})
    invoice_ids=set()
    for r in data_rows(book,'应收账款'):
        inv=val(book,'应收账款','A',r);need(inv and inv not in invoice_ids,'invoice_id_missing_or_duplicate');invoice_ids.add(inv)
        total=yuan(book,'应收账款',f'E{r}');paid=yuan(book,'应收账款',f'F{r}')
        need(0<=paid<=total,'invoice_collection_invalid')
        issued=dt('应收账款','C',r);due=dt('应收账款','D',r);need(issued<=due,'invoice_due_before_issue')
        linked=[b for b in bank.values() if b['kind']=='cash_in' and b['category']=='operating' and val(book,'银行流水','E',b['source_row'])==inv]
        need(sum((Decimal(b['amount']) for b in linked),Decimal('0.00'))==paid,'invoice_bank_collected_mismatch')
        if paid>0:
            received=dt('应收账款','G',r);need(received==max(b['settlement_date'] for b in linked),'invoice_payment_date_mismatch')
        rid=record('应收账款',r,'receivable_invoice','business-docs',amount=format(total,'.2f'),unit='CNY')
        for b in linked:links.append({'primary_record_id':b['record_id'],'corroborating_record_ids':[rid],'link_type':'document_reference','evidence_ref':'SYNTHETIC-INVOICE-BANK-REFERENCE'})
        if 0<paid<total:observations.append({'code':'normal_partial_receipt','source':f'应收账款!E{r}:F{r}','detail':'部分到账正常；按银行实际收款计现，应收总额不叠加。'})
    cash_rows=data_rows(book,'现金流水');need(len(cash_rows)==1,'single_period_demo_required')
    r=cash_rows[0];need(val(book,'现金流水','A',r)==context['period'],'monthly_context_mismatch')
    need(dt('现金流水','B',r)==binding.period_dates(context['period'])[1],'monthly_closing_date_mismatch')
    operating={side:sum((Decimal(b['amount']) for b in bank.values() if b['category']=='operating' and b['kind']==kind),Decimal('0.00')) for side,kind in [('inflow','cash_in'),('outflow','cash_out')]}
    need(yuan(book,'现金流水',f'D{r}')==operating['inflow'] and yuan(book,'现金流水',f'E{r}')==operating['outflow'],'monthly_operating_bank_mismatch')
    need(yuan(book,'现金流水',f'F{r}')==operating['inflow']-operating['outflow'],'monthly_operating_net_mismatch')
    need(val(book,'企业基本信息','B',25)==context['period']+'-01 至 '+binding.period_dates(context['period'])[1],'company_period_scope_mismatch')
    checks=['eight_sheet_contract','explicit_units','unique_bank_transactions','cash_directions_and_categories','explicit_settlement_dates',
            'business_bank_exact_links','noncash_financing_exclusion','invoice_bank_collected_comparison','monthly_operating_bank_reconciliation']
    return ({'schema_version':'mfg-cash-event-records.v1',**context,'origin':'offline_fixture','records':records},
           {'schema_version':'mfg-cash-link-decisions.v1',**context,'origin':'offline_fixture','duplicates':[],'transfers':[],'links':links},checks,observations)

def load_inputs():
    registration_raw=binding.small_file(BASE/'registration.json')
    registration=binding.parse(registration_raw)
    need(isinstance(registration,dict) and registration.get('schema_version')=='mfg-synthetic-eight-registration.v1','registration_schema_invalid')
    need(registration.get('source_id')==SOURCE_ID and registration.get('data_type')=='SYNTHETIC','synthetic_registration_required')
    expected={'account/context.json','account/scope.json','account/balances.json','account/evidence_manifest.json',
        'account/sources/opening.json','account/sources/closing.json','account_trust.json',
        'cash/scope_binding.json','cash/events.json','cash/decisions.json','cash/source_registry.json','cash_trust.json'}
    need(isinstance(registration.get('approved_files'),dict) and set(registration['approved_files'])==expected,'complete_evidence_bindings_required')
    need(WORKBOOK.is_file() and not WORKBOOK.is_symlink() and WORKBOOK.stat().st_size<1_000_000,'registered_workbook_unavailable')
    raw=WORKBOOK.read_bytes();digest=sha(raw);need(digest==registration.get('workbook_sha256'),'eight_sheet_workbook_changed_requires_rebinding')
    for name,digest_ in registration['approved_files'].items():need(sha(binding.small_file(binding.safe_file(BASE,name)))==digest_,'registered_evidence_file_changed:'+name)
    book=read_workbook(raw,SHAPE);context=registration['context']
    events,decisions,checks,observations=derive_events(book,digest,context)
    bound=binding.assess(BASE/'account',BASE/'account_trust.json',allow_fixture=True)
    need(bound['status']=='ready' and bound['fixture'] is True,'synthetic_account_balance_binding_not_ready')
    need(bound['context']==context,'registered_evidence_context_mismatch')
    frozen=binding.parse(binding.small_file(BASE/'cash/events.json'))
    need(canonical(events)==canonical(frozen),'workbook_normalized_cash_records_mismatch')
    need(canonical(decisions)==canonical(binding.parse(binding.small_file(BASE/'cash/decisions.json'))),'workbook_link_decisions_mismatch')
    need(canonical(bound)==canonical(binding.parse(binding.small_file(BASE/'cash/scope_binding.json'))),'account_binding_snapshot_stale')
    chosen=cash_selection.plan(BASE/'cash',BASE/'cash_trust.json',allow_fixture=True)
    need(chosen['status']=='ready' and chosen['fixture'] is True,'cash_selection_not_ready')
    need(chosen['context']==context,'cash_selection_context_mismatch')
    if WORKBOOK.read_bytes()!=raw:raise ValueError('workbook_changed_during_calculation')
    registration['_registration_file_sha256']=sha(registration_raw)
    return registration,book,bound,chosen,checks,observations

def compute(registration,book,bound,chosen,checks,observations):
    context=registration['context']
    with localcontext() as ctx:
        ctx.prec=60
        groups={g:{'inflow':Decimal('0.00'),'outflow':Decimal('0.00')} for g in ('operating','investing','financing')}
        for row in chosen['selected_records']:
            groups[row['category']]['inflow' if row['kind']=='cash_in' else 'outflow']+=binding.money(row['amount'])
        normalized={k:context[k] for k in ('tenant_id','company_id','period')}
        normalized.update(schema_version='mfg-program-input.v1',data_type='SYNTHETIC',unit='CNY',coverage='full_cashflow',
            account_scope='demo-bank-001',opening_source='synthetic-independent-opening-statement',closing_source='synthetic-independent-closing-statement',
            opening=bound['balance_totals']['opening'],actual_closing=bound['balance_totals']['independent_closing'])
        for g,values in groups.items():
            normalized[g+'_confirmed']='true'
            for k,v in values.items():normalized[g+'_'+k]=format(v,'.2f')
        input_sha=sha(canonical({'workbook':registration['workbook_sha256'],'approved_files':registration['approved_files']}))
        result=calculate_input(normalized,input_sha,SOURCE_ID)
        code_files=['eight_sheet_engine.py','account_binding.py','cash_selection.py','source_audit.py','program_engine.py']
        algorithm=sha(canonical({n:sha((ROOT/'src'/n).read_bytes()) for n in code_files}))
        result.update(calculation_version=VERSION,source_revision='eight-'+input_sha[:20],run_id='eight-'+sha(canonical({'input':input_sha,'algorithm':algorithm}))[:32])
        result['source'].update(sheet='八表及独立余额证据',range='银行流水!A3:L8')
        result['execution'].update(algorithm_sha256=algorithm,workbook_sha256=registration['workbook_sha256'],
            input_context=context,account_binding_sha256=bound['binding_sha256'],cash_selection_sha256=chosen['selection_sha256'])
        result['cashflow_groups']={g:{**{k:format(v,'.2f') for k,v in values.items()},'net':format(values['inflow']-values['outflow'],'.2f')} for g,values in groups.items()}
        result['evidence'].update(source_assurance='SYNTHETIC_DECLARED_NOT_REAL_BANK_EVIDENCE',
            selected_actual_record_count=len(chosen['selected_records']),selection_counts=chosen['counts'])
        # Sheet G is a stated balance, checked in addition to independently registered evidence.
        stated=yuan(book,'现金流水','G3');actual=binding.money(bound['balance_totals']['independent_closing'])
        need(stated==actual,'stated_and_independent_closing_mismatch')
    return {'status':'calculated','result':result,'fixture':True,'platform_native_verified':False,
        'can_save_source_result':result['reconciliation_state']=='matched','source_file_sha256':registration['workbook_sha256'],
        'database_written':False,'source_modified':False,'checked_sheets':list(book['blocks']),
        'executed_checks':checks+['independent_account_balance_binding','primary_cash_selection_and_no_double_count','all_three_cashflow_group_arithmetic','independent_closing_reconciliation'],
        'observations':observations,'scope':'registered_complete_synthetic_eight_sheet_demo_not_all_historical_gate_rules'}

def calculate():
    parts=load_inputs();out=compute(*parts)
    reg=parts[0]
    for name,digest in reg['approved_files'].items():need(sha(binding.small_file(binding.safe_file(BASE,name)))==digest,'evidence_changed_during_calculation')
    need(sha(WORKBOOK.read_bytes())==reg['workbook_sha256'],'workbook_changed_during_calculation')
    need(sha(binding.small_file(BASE/'registration.json'))==reg['_registration_file_sha256'],'registration_changed_during_calculation')
    return out

def inspect():
    reg,book,*_=load_inputs()
    return {'schema_version':'mfg-eight-sheet-inspection.v1','status':'inspected','source_id':SOURCE_ID,'data_type':'SYNTHETIC',
        'source_file_sha256':reg['workbook_sha256'],'sheet_names':list(book['blocks']),**book,'source_modified':False,
        'native_qwen_workbook':False}
