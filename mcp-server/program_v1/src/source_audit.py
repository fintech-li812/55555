"""Read all pinned sample sheets; actual checks and explicit, non-counting mapping preview."""
from datetime import date,timedelta
from decimal import Decimal,InvalidOperation,ROUND_HALF_UP,localcontext
import io,json,posixpath,re,zipfile
from pathlib import Path
import workbench as source

VERSION='manufacturing-source-audit.v1'
ROOT=Path(__file__).resolve().parents[1]
SHAPES={'企业基本信息':3,'现金流水':9,'应收账款':10,'筹资信息':5,
        '投资现金流':6,'银行流水':6,'行业参数':13,'数据字典':6}
ROLES={'企业基本信息':'主体/期间声明；不替代账户和银行余额证据',
       '现金流水':'经营月度汇总；与独立来源核对后才能合并',
       '应收账款':'开票/累计已收核对；不追加为独立现金',
       '筹资信息':'额度、贷款存量及还款计划；需实际收付明细',
       '投资现金流':'按状态保留投资候选收付；需账户/凭证关联',
       '银行流水':'本样例为回款取证；不与月度经营金额重复加总',
       '行业参数':'本期不启用高级评级/阈值模型，不计现金',
       '数据字典':'历史设计说明；不能代替实际数据检查'}

def read_all(source_id=source.SOURCE_ID):
    digest,raw=source._source_bytes(source_id)
    book=read_workbook(raw)
    if source._bound_source(source_id)!=digest:raise ValueError('source_changed_during_read')
    return dict(schema_version='mfg-eight-sheet-inspection.v1',status='inspected',
        source_id=source_id,source_kind='controlled_sample',source_file_sha256=digest,
        sheet_names=list(book['blocks']),**book,source_modified=False,
        read_method='local OOXML read-only; program audit; not Qwen native sheet API',
        money_conversion_performed=False,formula_execution_performed=False)

def read_workbook(raw,shapes=None):
    shapes=SHAPES if shapes is None else shapes
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        infos=z.infolist();names=[i.filename for i in infos]
        if len(infos)>1000 or len(names)!=len(set(names)) or sum(i.file_size for i in infos)>32_000_000:
            raise ValueError('workbook_container_limit')
        wb=source._xml(z,'xl/workbook.xml');rels=source._xml(z,'xl/_rels/workbook.xml.rels')
        targets={}
        for r in rels:
            if r.get('TargetMode')=='External':continue
            target=r.get('Target','')
            targets[r.get('Id')]=posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join('xl',target))
        strings=[]
        if 'xl/sharedStrings.xml' in names:
            strings=[''.join(t.text or '' for t in si.findall('.//s:t',source.NS)) for si in source._xml(z,'xl/sharedStrings.xml')]
        nodes=wb.findall('s:sheets/s:sheet',source.NS)
        titles=[n.get('name') for n in nodes]
        if len(titles)!=len(set(titles)) or set(titles)!=set(shapes):raise ValueError('eight_sheet_contract_mismatch')
        blocks={}
        for node in nodes:
            title=node.get('name');target=targets.get(node.get(source.REL_ID),'')
            if not re.fullmatch(r'xl/worksheets/[^/]+\.xml',target):raise ValueError('internal_sheet_required')
            cells={};last=0
            for c in source._xml(z,target).findall('.//s:sheetData/s:row/s:c',source.NS):
                address=c.get('r','');match=re.fullmatch(r'([A-Z]+)([1-9][0-9]*)',address)
                if not match or len(match[1])>1 or ord(match[1])-64>shapes[title] or int(match[2])>10000:
                    raise ValueError('source_cell_outside_supported_contract')
                if address in cells:raise ValueError('duplicate_source_cell')
                cells[address]=source._cell(c,strings);last=max(last,int(match[2]))
            blocks[title]={'range':f'A1:{chr(64+shapes[title])}{last}', 'cells':cells}
        props=wb.find('s:workbookPr',source.NS)
        system='1904' if props is not None and props.get('date1904') in ('1','true') else '1900'
    return {'date_system':system,'blocks':blocks}

def raw_value(book,sheet,address):return book['blocks'][sheet]['cells'].get(address,{}).get('value')
def data_rows(book,sheet):
    cells=book['blocks'][sheet]['cells']
    return sorted({int(re.search(r'[0-9]+$',a)[0]) for a,c in cells.items() if int(re.search(r'[0-9]+$',a)[0])>=3 and c.get('value') not in (None,'')})

def decimal_cell(book,sheet,address):
    c=book['blocks'][sheet]['cells'].get(address,{})
    if c.get('has_formula'):raise ValueError('unevaluated_formula_input')
    if c.get('cell_type') not in ('n','s','inlineStr'):raise ValueError('unsupported_amount_cell_type')
    v=c.get('value')
    if not isinstance(v,str) or not re.fullmatch(r'-?(?:0|[1-9][0-9]{0,19})(?:\.[0-9]{1,20})?',v):
        raise ValueError('missing_or_invalid_numeric_input')
    n=Decimal(v)
    if not n.is_finite():raise ValueError('nonfinite_input')
    return n

def yuan(book,sheet,address):
    with localcontext() as ctx:
        ctx.prec=60
        return (decimal_cell(book,sheet,address)*Decimal('10000')).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)

def parsed_date(book,sheet,address):
    c=book['blocks'][sheet]['cells'].get(address,{})
    if c.get('has_formula'):raise ValueError('unevaluated_formula_date')
    if c.get('cell_type') in ('b','e'):raise ValueError('unsupported_date_cell_type')
    v=c.get('value')
    if not isinstance(v,str) or not v:raise ValueError('missing_date')
    if re.fullmatch(r'[0-9]+(?:\.0+)?',v):
        n=int(Decimal(v))
        if n<0 or n>2000000 or (book['date_system']=='1900' and n in (0,60)):raise ValueError('invalid_excel_date')
        base=date(1904,1,1) if book['date_system']=='1904' else date(1899,12,31)
        if book['date_system']=='1900' and n>60:n-=1
        return base+timedelta(days=n)
    if not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',v):raise ValueError('date_format_not_supported')
    return date.fromisoformat(v)

def audit_book(book,digest):
    with localcontext() as ctx:
        ctx.prec=60
        return _audit_book(book,digest)

def _audit_book(book,digest):
    issues=[];observations=[];monthly=[];mapping=[];checks=[]
    def issue(code,sheet,address,detail,category='data_review',**extra):
        issues.append(dict(code=code,source=f'{sheet}!{address}',detail=detail,category=category,**extra))
    def number(sheet,address):
        try:return yuan(book,sheet,address)
        except (ValueError,InvalidOperation):
            issue('invalid_amount',sheet,address,'金额缺失、类型不支持或为未执行公式；不补零。','data_error')
            return None
    def when(sheet,address,required=True):
        if not required and raw_value(book,sheet,address) in (None,''):return None
        try:return parsed_date(book,sheet,address)
        except (ValueError,OverflowError):
            issue('invalid_date',sheet,address,'日期不能按明确的Excel日期系统或ISO日期读取。','data_error')
            return None
    # Field contracts are checked before money conversion: no guessed unit or column.
    headers={'现金流水':{'A2':'月份','D2':'经营性流入（万元）','E2':'经营性流出（万元）','F2':'净现金流（万元）','G2':'期末现金余额（万元）'},
        '应收账款':{'A2':'单据编号','E2':'应收金额（万元）','F2':'已收金额（万元）'},
        '银行流水':{'A2':'到账日期','C2':'到账金额（万元）','E2':'关联单据号'},
        '筹资信息':{'A2':'融资类型','C2':'金额/额度（万元）'},
        '投资现金流':{'C2':'发生/计划日期','D2':'金额（万元，支出为负）','E2':'状态'}}
    for sheet,fields in headers.items():
        for address,expected in fields.items():
            if raw_value(book,sheet,address)!=expected:
                issue('source_header_mismatch',sheet,address,'源字段或单位不匹配，停止本次金额映射。','data_error',expected=expected,observed=raw_value(book,sheet,address))
    if issues:return dict(schema_version=VERSION,status='blocked',source_file_sha256=digest,issues=issues,observations=[],checks=[{'code':'field_contract','status':'executed'}],mapping_preview=[],monthly_operating_preview=[],full_data_gate_executed=False,gate_rules_executed=True)
    checks.append({'code':'field_contract','status':'executed'})
    cash_rows=data_rows(book,'现金流水');seen={};months={}
    for r in cash_rows:
        signature=tuple(raw_value(book,'现金流水',f'{c}{r}') for c in 'ABCDEFGHI')
        if signature in seen:
            issue('duplicate_rows_preserved','现金流水',f'A{seen[signature]}:I{seen[signature]} / A{r}:I{r}',
                  '原始行完全相同，保留两行并等待明确处置；本次不删除、不合计重复候选。')
        else:seen[signature]=r
        period=raw_value(book,'现金流水',f'A{r}');d=when('现金流水',f'B{r}')
        if not isinstance(period,str) or not re.fullmatch(r'20[0-9]{2}-(?:0[1-9]|1[0-2])',period):
            issue('invalid_period','现金流水',f'A{r}','月份必须为YYYY-MM。','data_error')
        elif d and (d.strftime('%Y-%m')!=period or (d+timedelta(days=1)).day!=1):
            issue('period_date_mismatch','现金流水',f'A{r}:B{r}','月份与月末日期不匹配。','data_error')
        if isinstance(period,str):months.setdefault(period,[]).append(r)
        vals={c:number('现金流水',f'{c}{r}') for c in 'DEFG'}
        if vals['D'] is not None and vals['D']<0:issue('negative_gross_inflow','现金流水',f'D{r}','流入列为负，须确认交易及冲销口径。','data_error')
        if vals['E'] is not None and vals['E']<0:issue('negative_gross_outflow','现金流水',f'E{r}','流出列为负，须确认口径。','data_error')
        derived=None
        if all(vals[c] is not None for c in 'DEF'):
            derived=vals['D']-vals['E']
            if derived!=vals['F']:
                issue('operating_net_mismatch','现金流水',f'D{r}:F{r}',
                      '按表头单位读取的流入减流出与表内净额不一致；不反推或覆盖原值。','data_error',
                      computed_net_yuan=format(derived,'.2f'),reported_net_yuan=format(vals['F'],'.2f'))
        monthly.append({'source_row':r,'period':period,'inflow_yuan':None if vals['D'] is None else format(vals['D'],'.2f'),
            'outflow_yuan':None if vals['E'] is None else format(vals['E'],'.2f'),
            'reported_net_yuan':None if vals['F'] is None else format(vals['F'],'.2f'),
            'calculated_operating_net_yuan':None if derived is None else format(derived,'.2f'),
            'stated_closing_yuan':None if vals['G'] is None else format(vals['G'],'.2f'),
            'closing_is_independent_evidence':False,'counted_in_full_cashflow':False})
    valid_months=sorted(m for m in months if re.fullmatch(r'20[0-9]{2}-(?:0[1-9]|1[0-2])',m))
    for a,b in zip(valid_months,valid_months[1:]):
        ai=int(a[:4])*12+int(a[5:]);bi=int(b[:4])*12+int(b[5:])
        if bi-ai!=1:issue('monthly_gap','现金流水','A:A',f'已列月份在{a}与{b}之间断档。','data_error')
    declared=raw_value(book,'企业基本信息','B25')
    window=re.fullmatch(r'([0-9]{4}-[0-9]{2}-[0-9]{2}) 至 ([0-9]{4}-[0-9]{2}-[0-9]{2})',declared or '')
    if window:
        try:
            first,last=map(date.fromisoformat,window.groups())
            begin=first.year*12+first.month-1;end=last.year*12+last.month-1
            if first>last or end-begin>1200:raise ValueError('period_range_invalid')
            expected={f'{n//12:04d}-{n%12+1:02d}' for n in range(begin,end+1)}
            missing=sorted(expected-set(valid_months));extra=sorted(set(valid_months)-expected)
            if missing or extra:issue('declared_month_coverage_mismatch','现金流水','A:A',
                '月份未覆盖企业声明区间，或存在区间外记录。','data_error',missing_months=missing,extra_months=extra)
        except ValueError:issue('declared_period_invalid','企业基本信息','B25','声明的数据区间无效。','data_error')
    else:issue('declared_period_unconfirmed','企业基本信息','B25','数据区间无法按明确格式读取，不猜测应有月份。')
    # This is a source-specific registered annotation, not a numerical outlier threshold.
    if digest==source.SOURCE_SHA256:
        issue('unit_confirmation_required','现金流水','D8',
              '已登记的单位疑点：原值1631300，表头万元；1631300元等于163.13万元。未获依据前保留原值，不自动换口径。',
              observed=raw_value(book,'现金流水','D8'),rule_basis='pinned_source_annotation_not_statistical_threshold')
    checks.extend({'code':c,'status':'executed'} for c in ['monthly_fields_and_dates','exact_duplicate_rows','monthly_continuity','operating_net_arithmetic'])
    checks.append({'code':'declared_period_coverage','status':'executed'})
    checks.append({'code':'statistical_unit_outlier','status':'not_executed_policy_pending','detail':'倍数阈值未批准；不自造上下界。'})
    invoice={};partials=[]
    for r in data_rows(book,'应收账款'):
        key=raw_value(book,'应收账款',f'A{r}')
        if not key:issue('invoice_id_missing','应收账款',f'A{r}','缺少单据编号。','data_error')
        elif key in invoice:issue('duplicate_invoice_id','应收账款',f'A{r}','单据编号重复，保留原行。','data_review')
        due=number('应收账款',f'E{r}');collected=number('应收账款',f'F{r}')
        issued=when('应收账款',f'C{r}');expiry=when('应收账款',f'D{r}')
        received=when('应收账款',f'G{r}',required=collected is not None and collected>0)
        if issued and expiry and expiry<issued:issue('due_before_invoice','应收账款',f'C{r}:D{r}','到期日早于开票日。','data_error')
        if due is not None and due<0 or collected is not None and collected<0:issue('negative_receivable','应收账款',f'E{r}:F{r}','负数应收或已收须确认退款/冲销口径。')
        if due is not None and collected is not None and collected>due:issue('receivable_over_collected','应收账款',f'E{r}:F{r}','累计已收超过应收，需核对预收/冲销及来源。')
        if due is not None and collected is not None and 0<collected<due:
            partials.append(r)
            observations.append({'code':'normal_partial_receipt','source':f'应收账款!E{r}:F{r}',
                'detail':'部分到账是正常业务状态，不因少于应收金额判错。'})
        if key and key not in invoice:invoice[key]={'row':r,'due':due,'collected':collected,'customer':raw_value(book,'应收账款',f'B{r}')}
        mapping.append({'sheet':'应收账款','row':r,'kind':'receivable_reconciliation_only','counted':False,
                        'reason':'应收或累计已收不是可直接追加的独立本期现金流水。'})
    bank_totals={}
    for r in data_rows(book,'银行流水'):
        d=when('银行流水',f'A{r}');amount=number('银行流水',f'C{r}');key=raw_value(book,'银行流水',f'E{r}')
        if amount is not None and amount<0:issue('negative_bank_receipt','银行流水',f'C{r}','到账金额为负，须确认交易类型。')
        if key not in invoice:issue('bank_invoice_link_missing','银行流水',f'E{r}','关联单据在应收表中未找到；不推断匹配。')
        elif amount is not None:bank_totals[key]=bank_totals.get(key,Decimal('0.00'))+amount
        mapping.append({'sheet':'银行流水','row':r,'kind':'actual_receipt_evidence','candidate_category':'operating',
            'settlement_date':None if d is None else d.isoformat(),'amount_yuan':None if amount is None else format(amount,'.2f'),
            'counted':False,'reason':'与经营月表/应收核对，不重复叠加；账户及来源主从覆盖仍需绑定。'})
    for key,total in bank_totals.items():
        ref=invoice[key];collected=ref['collected']
        if collected is not None and total!=collected:
            issue('bank_vs_recorded_collected_difference','应收账款',f'F{ref["row"]}',
                '所列银行到账合计与应收表累计已收不同；需核对时间范围/分笔凭证，不按少于应收本身判错。',
                linked_invoice=key,listed_bank_yuan=format(total,'.2f'),recorded_collected_yuan=format(collected,'.2f'))
    checks.extend({'code':c,'status':'executed'} for c in ['receivable_amounts_and_dates','invoice_duplicate_ids','bank_link_and_collected_comparison'])
    for r in data_rows(book,'筹资信息'):
        typ=raw_value(book,'筹资信息',f'A{r}');amount=number('筹资信息',f'C{r}');when('筹资信息',f'E{r}')
        if amount is not None and amount<0:issue('negative_financing_amount','筹资信息',f'C{r}','额度/金额为负，不能用于融资容量或现金流计算。','data_error')
        reason='缺少实际放款/还本付息日期及独立流水，不能把余额、额度或计划当本期现金。'
        if typ=='银行授信额度':reason='授信额度不是实际收到现金，排除现金合计。'
        if typ=='还款计划':reason='计划与到期日不代表已实际支付，单列待核。'
        mapping.append({'sheet':'筹资信息','row':r,'kind':typ,'candidate_category':'financing','counted':False,'reason':reason})
    for r in data_rows(book,'投资现金流'):
        d=when('投资现金流',f'C{r}');amount=number('投资现金流',f'D{r}');status=raw_value(book,'投资现金流',f'E{r}')
        mapping.append({'sheet':'投资现金流','row':r,'candidate_category':'investing','source_status':status,
            'cash_claim':'paid_in_source' if status=='已支付' else 'settlement_not_confirmed',
            'candidate_direction':None if amount is None else 'outflow' if amount<0 else 'inflow',
            'signed_amount_yuan':None if amount is None else format(amount,'.2f'),
            'date':None if d is None else d.isoformat(),'counted':False,
            'reason':'源表已支付/已完成分别保留；未绑定账户与实际收付凭证，不形成完整现金结果。'})
    checks.extend({'code':c,'status':'executed'} for c in ['financing_amount_and_maturity','investment_amount_date_status_mapping'])
    observations.append({'code':'static_gate_labels_not_used','source':'现金流水!I:I / 应收账款!J:J',
                         'detail':'原静态标签或空白不充当本次检查结果；新报告独立输出，不回写原表。'})
    observations.append({'code':'historical_dictionary_not_live_findings','source':'数据字典',
                         'detail':'当前字典标题为制造业共6处；历史17处跨行业清单及字典旧数值不作当前实测结论。'})
    return dict(schema_version=VERSION,status='audited',source_file_sha256=digest,issues=issues,
        observations=observations,checks=checks,gate_rules_executed=True,full_data_gate_executed=False,
        normal_partial_receipts_treated_as_errors=False,normal_partial_receipt_rows=partials,
        monthly_operating_preview=monthly,mapping_preview=mapping,
        sheet_data_row_counts={s:len(data_rows(book,s)) for s in SHAPES},
        cashflow_aggregation_performed=False,source_modified=False,
        remaining_checks=[{'code':'full_balance_reconciliation','status':'not_executed_missing_scope_and_balances'},
                          {'code':'all_historical_gate_rules','status':'not_claimed_complete'}])

def audit_source(source_id=source.SOURCE_ID):
    inspected=read_all(source_id)
    out=audit_book(inspected,inspected['source_file_sha256'])
    out.update(source_id=source_id,source_kind='controlled_sample',sheet_names=inspected['sheet_names'])
    out['sheet_roles']=ROLES
    if source._bound_source(source_id)!=inspected['source_file_sha256']:raise ValueError('source_changed_during_audit')
    return out

def account_binding_status(source_id=source.SOURCE_ID):
    if source_id!=source.SOURCE_ID:raise ValueError('original_source_required')
    import account_binding as module
    raw=(ROOT/'bindings/registry.json').read_bytes();data=module.parse(raw)
    row=data.get(source_id)
    if not isinstance(row,dict) or row.get('source_sha256')!=source._bound_source(source_id):raise ValueError('binding_registry_source_mismatch')
    if row.get('state')=='not_registered':
        return {'status':'blocked','module_version':module.VERSION,'module_connected':True,
                'scope_ready':False,'balance_binding_ready':False,'opening_ready':False,'independent_closing_ready':False,
                'issues':[{'code':c,'category':'missing_evidence','detail':detail} for c,detail in [
                    ('account_scope_missing','需登记纳入账户、统计主体和月份范围。'),
                    ('opening_balance_not_bound','需提供同账户范围的期初余额及来源记录。'),
                    ('independent_closing_not_bound','需提供同账户范围的独立实际期末及来源记录，不能使用计算期末。')]]}
    if row.get('state')!='registered':raise ValueError('binding_registry_state_invalid')
    def safe(name):
        if not isinstance(name,str) or not name or '\\' in name or ':' in name:raise ValueError('binding_path_invalid')
        p=(ROOT/'bindings'/name).resolve()
        if not p.is_relative_to((ROOT/'bindings').resolve()):raise ValueError('binding_path_outside_root')
        return p
    bundle=safe(row.get('bundle'));trust=safe(row.get('trust_file'))
    if row.get('trust_sha256')!=module.sha(module.small_file(trust)):raise ValueError('binding_trust_digest_mismatch')
    result=module.assess(bundle,trust,allow_fixture=row.get('allow_fixture') is True)
    if not isinstance(row.get('context'),dict) or result.get('context')!=row['context']:
        raise ValueError('binding_context_not_bound_to_registered_source')
    if (ROOT/'bindings/registry.json').read_bytes()!=raw:raise ValueError('binding_registration_changed_during_read')
    result['module_connected']=True
    return result

def cash_selection_status(source_id,bound):
    import cash_selection as module
    if source_id!=source.SOURCE_ID:raise ValueError('original_source_required')
    pending={'status':'blocked','module_version':module.VERSION,'module_connected':True,
             'cashflow_totals':None,'can_save_source_result':False,'issues':[]}
    if bound.get('status')!='ready':
        pending['issues']=[{'code':'cash_selection_requires_account_binding','category':'mapping_review',
            'detail':'现金主来源、账户内转和跨表防重计模块已接入；须先有通过的账户/余额绑定。'}]
        return pending
    import account_binding as binding
    raw=(ROOT/'bindings/registry.json').read_bytes();row=binding.parse(raw)[source_id]
    if not row.get('cash_bundle') or not row.get('cash_trust_file'):
        pending['issues']=[{'code':'cash_source_registration_missing','category':'mapping_review',
            'detail':'需登记实际收付主来源、覆盖范围及跨源/内转关联；不把应收、额度和计划相加。'}]
        return pending
    def path(name):
        return binding.safe_file(ROOT/'bindings',name)
    bundle=path(row['cash_bundle']);trust=path(row['cash_trust_file'])
    if binding.sha(binding.small_file(trust))!=row.get('cash_trust_sha256'):raise ValueError('cash_trust_digest_mismatch')
    result=module.plan(bundle,trust,allow_fixture=row.get('allow_fixture') is True)
    if result.get('context')!=bound.get('context'):raise ValueError('cash_and_account_context_mismatch')
    if (ROOT/'bindings/registry.json').read_bytes()!=raw:raise ValueError('cash_registration_changed_during_read')
    result['module_connected']=True
    return result
