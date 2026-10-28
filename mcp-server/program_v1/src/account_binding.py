"""Account scope and independently registered balance evidence, without cashflow inference."""
from pathlib import Path
from datetime import date
from decimal import Decimal, localcontext
import calendar,hashlib,json,re

VERSION='mfg-account-balance-binding.v1'
CONTEXT=('tenant_id','company_id','period','source_revision','run_id')
FILES=('context.json','scope.json','balances.json','evidence_manifest.json')
MONEY=re.compile(r'-?(?:0|[1-9][0-9]{0,17})\.[0-9]{2}')
SHA=re.compile('[0-9a-f]{64}')
MAX_INPUT=1_000_000
MAX_SOURCE=2_000_000
MAX_ACCOUNTS=1000
MAX_RECORDS=2000

class BindingError(ValueError):
    """Stable diagnostic code; never echo evidence contents or paths."""
    def __init__(self,code):super().__init__(code);self.code=code
def require(condition,code):
    if not condition:raise BindingError(code)
def canonical(obj):return json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')
def sha(raw):return hashlib.sha256(raw).hexdigest()
def parse(raw):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise BindingError('DUPLICATE_JSON_KEY')
            result[key]=value
        return result
    try:return json.loads(raw.decode('utf-8-sig'),object_pairs_hook=unique,
                          parse_constant=lambda value:(_ for _ in ()).throw(BindingError('NON_FINITE_JSON')))
    except (UnicodeError,json.JSONDecodeError):raise BindingError('INVALID_JSON')
def small_file(path,limit=MAX_INPUT):
    try:
        require(path.is_file() and not path.is_symlink() and not getattr(path,'is_junction',lambda:False)(),'FILE_MISSING_OR_LINK')
        require(path.stat().st_size<=limit,'FILE_SIZE_LIMIT')
        raw=path.read_bytes();require(len(raw)<=limit,'FILE_SIZE_LIMIT');return raw
    except OSError:raise BindingError('FILE_UNREADABLE')
def safe_file(root,name):
    require(isinstance(name,str) and name and '\\' not in name and ':' not in name,'UNSAFE_SOURCE_PATH')
    segments=name.split('/')
    require(all(part not in ('','.','..') and not part.endswith((' ','.')) for part in segments),'UNSAFE_SOURCE_PATH')
    rel=Path(name);require(not rel.is_absolute(),'UNSAFE_SOURCE_PATH')
    p=root/rel
    require(p.resolve().is_relative_to(root.resolve()),'SOURCE_PATH_ESCAPE')
    for item in [p,*p.parents]:
        if item==root.parent:break
        require(not item.is_symlink() and not getattr(item,'is_junction',lambda:False)(),'SOURCE_PATH_LINK')
    return p
def identifier(value):return isinstance(value,str) and bool(value.strip()) and len(value)<=128
def context(value,trusted=None):
    require(isinstance(value,dict),'CONTEXT_REQUIRED')
    for key in CONTEXT:
        require(identifier(value.get(key)),'CONTEXT_FIELD_REQUIRED')
        if trusted is not None:require(value[key]==trusted[key],'CONTEXT_MISMATCH')
    require(re.fullmatch(r'[0-9]{4}-(?:0[1-9]|1[0-2])',value['period']) is not None,'PERIOD_INVALID')
    require(1<=int(value['period'][:4])<=9999,'PERIOD_INVALID')
    return {key:value[key] for key in CONTEXT}
def period_dates(period):
    year,month=map(int,period.split('-'))
    start=date(year,month,1)
    require(start.toordinal()>1,'PERIOD_OPENING_DATE_UNSUPPORTED')
    return date.fromordinal(start.toordinal()-1).isoformat(),date(year,month,calendar.monthrange(year,month)[1]).isoformat()
def money(value):
    require(isinstance(value,str) and MONEY.fullmatch(value) is not None,'BALANCE_AMOUNT_INVALID')
    return Decimal(value)
def sum_money(values):
    with localcontext() as ctx:
        ctx.prec=40
        value=sum((money(v) for v in values),Decimal('0.00'))
        require(abs(value)<Decimal('1000000000000000000'),'BALANCE_TOTAL_OVERFLOW')
        return format(value,'.2f')
def unique_id(items,key,code):
    require(isinstance(items,list),code)
    result={}
    for item in items:
        require(isinstance(item,dict) and identifier(item.get(key)),code)
        require(item[key] not in result,code);result[item[key]]=item
    return result
def assess(bundle,trust_file,*,allow_fixture=False):
    """Use external operator binding. Digest agreement does not authenticate documents."""
    base={'schema_version':VERSION,'status':'invalid','scope_ready':False,'balance_binding_ready':False,
          'can_save_source_result':False,'can_claim_full_cashflow':False,
          'native_sheet_verified':False,'source_authenticity_verified':False,
          'scope_summary':None,'balance_totals':None,'accounts':[],'issues':[],'evidence_checks':[],
          'mutations_performed':False,'context':None}
    def issue(code,level='blocked',account_id=None):
        item={'code':code,'level':level}
        if account_id is not None:item['account_id']=account_id
        base['issues'].append(item)
    try:
        bundle=Path(bundle);trust_file=Path(trust_file)
        require(bundle.is_dir() and not bundle.is_symlink() and not getattr(bundle,'is_junction',lambda:False)(),'BUNDLE_DIRECTORY_INVALID')
        require(not trust_file.resolve().is_relative_to(bundle.resolve()),'TRUST_MUST_BE_EXTERNAL')
        trust_raw=small_file(trust_file)
        trust=parse(trust_raw)
        require(isinstance(trust,dict) and trust.get('schema_version')=='mfg-account-binding-trust.v1','TRUST_SCHEMA_INVALID')
        ctx=context(trust);base['context']=ctx
        require(trust.get('industry')=='manufacturing','INDUSTRY_OUT_OF_SCOPE')
        require(identifier(trust.get('approval_ref')),'OPERATOR_APPROVAL_REFERENCE_REQUIRED')
        approved=trust.get('approved_inputs')
        require(isinstance(approved,dict) and set(approved)==set(FILES),'TRUST_INPUT_BINDINGS_INCOMPLETE')
        for value in approved.values():require(isinstance(value,str) and SHA.fullmatch(value) is not None,'TRUST_HASH_INVALID')
        blobs={};inputs={}
        for name in FILES:
            blobs[name]=small_file(safe_file(bundle,name))
            require(sha(blobs[name])==approved[name],'INPUT_APPROVAL_HASH_MISMATCH')
            inputs[name]=parse(blobs[name])
            context(inputs[name],ctx)
        require(inputs['context.json'].get('industry')=='manufacturing','INDUSTRY_OUT_OF_SCOPE')
        scope=inputs['scope.json'];balances=inputs['balances.json'];manifest=inputs['evidence_manifest.json']
        for value,version in [(scope,'mfg-account-scope.v1'),(balances,'mfg-balance-observations.v1'),(manifest,'mfg-balance-evidence-manifest.v1')]:
            require(value.get('schema_version')==version,'INPUT_SCHEMA_INVALID')
        origins=[value.get('origin') for value in [scope,balances,manifest]]
        require(len(set(origins))==1 and origins[0] in ('offline_fixture','operator_registered_evidence'),'ORIGIN_MISMATCH')
        fixture=origins[0]=='offline_fixture'
        require(not fixture or (allow_fixture and trust.get('allow_fixture') is True),'FIXTURE_NOT_EXPLICITLY_ALLOWED')
        base['fixture']=fixture
        require(scope.get('unit')=='CNY' and balances.get('unit')=='CNY','UNSUPPORTED_UNIT')
        opening_date,closing_date=period_dates(ctx['period'])
        require(scope.get('opening_as_of')==opening_date and scope.get('closing_as_of')==closing_date,'PERIOD_BALANCE_CUTOFF_MISMATCH')
        require(scope.get('scope_confirmed') in (True,False) and type(scope.get('scope_confirmed')) is bool,'SCOPE_CONFIRMATION_INVALID')
        if not scope['scope_confirmed']:issue('ACCOUNT_SCOPE_NOT_CONFIRMED')
        require(identifier(scope.get('reporting_entity_id')),'REPORTING_ENTITY_REQUIRED')
        accounts=unique_id(scope.get('accounts'),'account_id','ACCOUNT_ID_DUPLICATE_OR_INVALID')
        require(len(accounts)<=MAX_ACCOUNTS,'ACCOUNT_COUNT_LIMIT')
        included={}
        for aid,account in accounts.items():
            require(account.get('reporting_entity_id')==scope['reporting_entity_id'],'ACCOUNT_ENTITY_MISMATCH')
            require(type(account.get('included')) is bool,'ACCOUNT_INCLUSION_REQUIRED')
            require('account_number' not in account and 'iban' not in account,'RAW_ACCOUNT_NUMBER_NOT_ALLOWED')
            if not account['included']:
                require(identifier(account.get('exclusion_reason')),'EXCLUDED_ACCOUNT_REASON_REQUIRED')
                continue
            included[aid]=account
            if account.get('currency')!='CNY':issue('ACCOUNT_CURRENCY_UNSUPPORTED',account_id=aid)
        if not included:issue('NO_INCLUDED_ACCOUNTS')
        base['scope_summary']={'reporting_entity_id':scope['reporting_entity_id'],'included_accounts':len(included),'excluded_accounts':len(accounts)-len(included),'opening_as_of':opening_date,'closing_as_of':closing_date,'unit':'CNY'}
        entries=unique_id(manifest.get('sources'),'evidence_id','EVIDENCE_ID_DUPLICATE_OR_INVALID')
        require(len(entries)<=MAX_RECORDS,'EVIDENCE_COUNT_LIMIT')
        paths=set();source_records={};source_blobs={}
        for evidence_id,entry in entries.items():
            path=entry.get('path');file=safe_file(bundle,path)
            require(path not in paths,'EVIDENCE_PATH_DUPLICATE');paths.add(path)
            require(entry.get('source_class') in ('bank_statement_extract','reconciled_ledger_extract'),'INDEPENDENT_SOURCE_CLASS_REQUIRED')
            require(entry.get('independence')=='operator_approved_independent_actual','INDEPENDENCE_APPROVAL_REQUIRED')
            require(identifier(entry.get('approval_ref')),'EVIDENCE_APPROVAL_REFERENCE_REQUIRED')
            require(isinstance(entry.get('sha256'),str) and SHA.fullmatch(entry['sha256']) is not None,'EVIDENCE_HASH_INVALID')
            raw=small_file(file,MAX_SOURCE);source_blobs[path]=raw
            require(sha(raw)==entry['sha256'],'EVIDENCE_HASH_MISMATCH')
            source=parse(raw)
            require(isinstance(source,dict) and source.get('schema_version')=='mfg-balance-source-records.v1','SOURCE_RECORD_SCHEMA_INVALID')
            context(source,ctx)
            require(source.get('origin')==origins[0] and source.get('source_class')==entry['source_class'],'EVIDENCE_PROVENANCE_MISMATCH')
            require(source.get('derivation')=='registered_independent_actual','COMPUTED_BALANCE_NOT_INDEPENDENT')
            records=unique_id(source.get('records'),'source_record_id','SOURCE_RECORD_ID_DUPLICATE_OR_INVALID')
            require(len(records)<=MAX_RECORDS,'SOURCE_RECORD_COUNT_LIMIT')
            source_records[evidence_id]=records
            base['evidence_checks'].append({'evidence_id':evidence_id,'sha256':entry['sha256'],'hash_matches':True,'independence':'operator_registration_only_not_independent_authentication'})
        observations=unique_id(balances.get('records'),'record_id','BALANCE_RECORD_ID_DUPLICATE_OR_INVALID')
        require(len(observations)<=MAX_RECORDS,'BALANCE_RECORD_COUNT_LIMIT')
        mapped={};used=set()
        for rid,item in observations.items():
            aid=item.get('account_id');require(aid in accounts,'BALANCE_ACCOUNT_NOT_REGISTERED')
            kind=item.get('kind');require(kind in ('opening','closing'),'BALANCE_KIND_INVALID')
            require(item.get('as_of')==(opening_date if kind=='opening' else closing_date),'BALANCE_DATE_MISMATCH')
            require(item.get('unit')=='CNY','BALANCE_UNIT_INVALID')
            key=(aid,kind);require(key not in mapped,'DUPLICATE_ACCOUNT_BALANCE')
            amount=item.get('amount')
            if amount is None:
                issue('BALANCE_VALUE_MISSING',account_id=aid);mapped[key]=None;continue
            money(amount)
            evidence_id=item.get('evidence_id');ref=item.get('source_record_id')
            require(evidence_id in source_records and ref in source_records[evidence_id],'BALANCE_EVIDENCE_REFERENCE_MISSING')
            pair=(evidence_id,ref);require(pair not in used,'SOURCE_RECORD_REUSED');used.add(pair)
            evidence=source_records[evidence_id][ref]
            for field in ['account_id','kind','as_of','amount','unit']:
                require(evidence.get(field)==item.get(field),'BALANCE_SOURCE_VALUE_MISMATCH')
            require(identifier(evidence.get('source_location')),'SOURCE_LOCATION_MISSING')
            mapped[key]=amount
        for aid in included:
            detail={'account_id':aid,'opening':None,'independent_closing':None}
            for kind,key in [('opening','opening'),('closing','independent_closing')]:
                if (aid,kind) not in mapped or mapped[(aid,kind)] is None:issue('OPENING_BALANCE_MISSING' if kind=='opening' else 'INDEPENDENT_CLOSING_BALANCE_MISSING',account_id=aid)
                else:detail[key]=mapped[(aid,kind)]
            base['accounts'].append(detail)
        base['scope_ready']=scope['scope_confirmed'] and bool(included) and not any(i['code']=='ACCOUNT_CURRENCY_UNSUPPORTED' for i in base['issues'])
        base['status']='blocked' if base['issues'] else 'ready'
        base['balance_binding_ready']=base['status']=='ready'
        if base['balance_binding_ready']:
            base['balance_totals']={'opening':sum_money([mapped[(aid,'opening')] for aid in included]),'independent_closing':sum_money([mapped[(aid,'closing')] for aid in included]),'unit':'CNY'}
        # Detect source changes before returning; do not persist stale signed bindings.
        for name,raw in blobs.items():require(small_file(safe_file(bundle,name))==raw,'INPUT_CHANGED_DURING_CHECK')
        for name,raw in source_blobs.items():require(small_file(safe_file(bundle,name),MAX_SOURCE)==raw,'SOURCE_CHANGED_DURING_CHECK')
        require(small_file(trust_file)==trust_raw,'TRUST_CHANGED_DURING_CHECK')
        base['binding_sha256']=sha(canonical({'approved_inputs':approved,'context':ctx,'approval_ref':trust['approval_ref']}))
        base['limitations']=['Source hashes bind operator-selected records; authenticity and authority are not independently verified.',
            'No cashflow completeness, native recalculation or database save is authorized by this binding.']
    except BindingError as exc:
        base.update(status='invalid',scope_ready=False,balance_binding_ready=False,balance_totals=None,accounts=[],evidence_checks=[])
        issue(exc.code,'invalid')
    except (OSError,ValueError,TypeError,KeyError,OverflowError):
        base.update(status='invalid',scope_ready=False,balance_binding_ready=False,balance_totals=None,accounts=[],evidence_checks=[])
        issue('BINDING_INPUT_INVALID','invalid')
    return base

