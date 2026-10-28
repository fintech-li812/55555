"""Explicit cash-evidence selection and approved transfer elimination; no fuzzy matching."""
from pathlib import Path
from datetime import date
import re
from account_binding import BindingError,require,small_file,safe_file,parse,sha,canonical,context,identifier,money,unique_id

VERSION='mfg-cash-evidence-selection.v1'
FILES=('scope_binding.json','events.json','source_registry.json','decisions.json')
CASH={'cash_in','cash_out'}
NONCASH={'receivable_invoice','credit_limit','noncash_adjustment'}
GROUPS={'operating','investing','financing'}
def plan(bundle,trust_file,*,allow_fixture=False):
    out={'schema_version':VERSION,'status':'invalid','context':None,'fixture':False,
         'can_claim_full_cashflow':False,'can_save_source_result':False,
         'source_authenticity_verified':False,'native_sheet_verified':False,
         'selected_records':[],'record_decisions':[],'approved_transfer_pairs':[],
         'issues':[],'counts':None,'cashflow_totals':None,'mutations_performed':False}
    def issue(code,record_id=None):
        row={'code':code}
        if record_id:row['record_id']=record_id
        out['issues'].append(row)
    try:
        bundle=Path(bundle);trust_file=Path(trust_file)
        require(bundle.is_dir() and not bundle.is_symlink(),'BUNDLE_DIRECTORY_INVALID')
        require(not trust_file.resolve().is_relative_to(bundle.resolve()),'TRUST_MUST_BE_EXTERNAL')
        traw=small_file(trust_file);trust=parse(traw)
        require(isinstance(trust,dict) and trust.get('schema_version')=='mfg-cash-selection-trust.v1','TRUST_SCHEMA_INVALID')
        ctx=context(trust);out['context']=ctx
        require(trust.get('industry')=='manufacturing' and identifier(trust.get('approval_ref')),'OPERATOR_REGISTRATION_REQUIRED')
        approved=trust.get('approved_inputs');require(isinstance(approved,dict) and set(approved)==set(FILES),'INPUT_BINDINGS_REQUIRED')
        raw={};data={}
        for name in FILES:
            require(isinstance(approved[name],str) and re.fullmatch('[0-9a-f]{64}',approved[name]) is not None,'INPUT_HASH_INVALID')
            raw[name]=small_file(safe_file(bundle,name));require(sha(raw[name])==approved[name],'INPUT_APPROVAL_HASH_MISMATCH')
            data[name]=parse(raw[name])
            context(data[name].get('context') if name=='scope_binding.json' and isinstance(data[name],dict) else data[name],ctx)
        scope=data['scope_binding.json'];events=data['events.json'];registry=data['source_registry.json'];decisions=data['decisions.json']
        require(scope.get('schema_version')=='mfg-account-balance-binding.v1','SCOPE_BINDING_SCHEMA_INVALID')
        require(events.get('schema_version')=='mfg-cash-event-records.v1','EVENT_SCHEMA_INVALID')
        require(registry.get('schema_version')=='mfg-cash-source-registry.v1','REGISTRY_SCHEMA_INVALID')
        require(decisions.get('schema_version')=='mfg-cash-link-decisions.v1','DECISIONS_SCHEMA_INVALID')
        origins=[x.get('origin') for x in [events,registry,decisions]]
        require(len(set(origins))==1 and origins[0] in ('offline_fixture','operator_registered_evidence'),'ORIGIN_MISMATCH')
        fixture=origins[0]=='offline_fixture';out['fixture']=fixture
        require(not fixture or (allow_fixture and trust.get('allow_fixture') is True),'FIXTURE_NOT_EXPLICITLY_ALLOWED')
        require(scope.get('fixture') is fixture,'SCOPE_ORIGIN_MISMATCH')
        if scope.get('scope_ready') is not True:issue('ACCOUNT_SCOPE_NOT_READY')
        summary=scope.get('scope_summary');require(isinstance(summary,dict) and summary.get('unit')=='CNY','SCOPE_UNIT_INVALID')
        accounts=unique_id(scope.get('accounts'),'account_id','SCOPE_ACCOUNTS_INVALID')
        require(0<len(accounts)<=1000,'ACCOUNT_COUNT_INVALID')
        require(summary.get('included_accounts')==len(accounts),'ACCOUNT_SCOPE_COUNT_MISMATCH')
        sources=unique_id(registry.get('sources'),'source_id','SOURCE_REGISTRY_INVALID')
        require(len(sources)<=2000,'SOURCE_COUNT_LIMIT')
        owners={aid:[] for aid in accounts}
        for sid,s in sources.items():
            require(s.get('role') in ('primary_actual','corroborating','noncash'),'SOURCE_ROLE_INVALID')
            ids=s.get('account_ids');require(isinstance(ids,list) and len(ids)==len(set(ids)) and all(aid in accounts for aid in ids),'SOURCE_ACCOUNT_OUT_OF_SCOPE')
            require(s.get('period')==ctx['period'],'SOURCE_PERIOD_MISMATCH')
            if s['role']=='primary_actual':
                require(ids,'PRIMARY_ACCOUNT_REQUIRED')
                require(type(s.get('complete')) is bool and type(s.get('declared_zero')) is bool,'SOURCE_COMPLETENESS_REQUIRED')
                if not s['complete']:issue('PRIMARY_SOURCE_INCOMPLETE')
                for aid in ids:owners[aid].append(sid)
        for aid,ids in owners.items():
            if not ids:issue('PRIMARY_SOURCE_MISSING',aid)
            elif len(ids)>1:issue('PRIMARY_SOURCE_AMBIGUOUS',aid)
        rows=unique_id(events.get('records'),'record_id','EVENT_RECORD_ID_DUPLICATE_OR_INVALID')
        require(len(rows)<=20000,'EVENT_COUNT_LIMIT')
        source_keys=set();primary={};actions={};cash_counts={sid:0 for sid in sources}
        for rid,row in rows.items():
            sid=row.get('source_id');require(sid in sources,'EVENT_SOURCE_NOT_REGISTERED')
            source=sources[sid];kind=row.get('kind');require(kind in CASH|NONCASH,'EVENT_KIND_INVALID')
            key=row.get('source_record_key');require(isinstance(key,str) and re.fullmatch('[0-9a-f]{64}',key) is not None,'SOURCE_RECORD_KEY_INVALID')
            require((key,kind) not in source_keys,'SOURCE_RECORD_KEY_DUPLICATED');source_keys.add((key,kind))
            if kind in NONCASH:
                actions[rid]={'action':'noncash_excluded','reason':kind};continue
            require(source['role']!='noncash','CASH_RECORD_IN_NONCASH_SOURCE')
            require(row.get('unit')=='CNY','CASH_UNIT_INVALID');require(money(row.get('amount'))>=0,'GROSS_CASH_NEGATIVE')
            aid=row.get('account_id');require(aid in accounts and aid in source['account_ids'],'EVENT_ACCOUNT_OUT_OF_SCOPE')
            d=row.get('settlement_date');require(isinstance(d,str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',d) is not None,'CASH_DATE_INVALID')
            try:date.fromisoformat(d)
            except ValueError:raise BindingError('CASH_DATE_INVALID')
            require(d[:7]==ctx['period'],'CASH_DATE_OUT_OF_PERIOD')
            require(type(row.get('internal_transfer_hint',False)) is bool,'TRANSFER_HINT_INVALID')
            if source['role']=='primary_actual':
                require(identifier(row.get('transaction_id')),'PRIMARY_TRANSACTION_ID_REQUIRED')
                primary[rid]=row;cash_counts[sid]+=1;actions[rid]={'action':'count_external_actual','reason':'registered_primary_source'}
            else:actions[rid]={'action':'corroborating_only','reason':'not_primary_actual_source'}
        for sid,source in sources.items():
            if source['role']!='primary_actual':continue
            if cash_counts[sid]==0 and source['declared_zero'] is not True:issue('EMPTY_PRIMARY_NOT_CONFIRMED_ZERO')
            if cash_counts[sid]>0 and source['declared_zero']:issue('ZERO_DECLARATION_CONTRADICTS_RECORDS')
        duplicates=decisions.get('duplicates',[]);require(isinstance(duplicates,list),'DUPLICATE_DECISION_INVALID')
        suppressed=set();kept=set()
        for decision in duplicates:
            require(isinstance(decision,dict) and identifier(decision.get('evidence_ref')),'DUPLICATE_APPROVAL_REQUIRED')
            keep=decision.get('keep_record_id');others=decision.get('duplicate_record_ids')
            require(keep in primary and isinstance(others,list) and others and len(others)==len(set(others)),'DUPLICATE_DECISION_INVALID')
            require(keep not in suppressed,'DUPLICATE_KEEP_ALREADY_SUPPRESSED')
            kept.add(keep)
            for duplicate in others:
                require(duplicate in primary and duplicate!=keep and duplicate not in suppressed and duplicate not in kept,'DUPLICATE_DECISION_REUSED')
                for field in ['account_id','kind','settlement_date','unit','amount','transaction_id','category']:
                    require(primary[keep].get(field)==primary[duplicate].get(field),'DUPLICATE_RECORDS_NOT_IDENTICAL')
                suppressed.add(duplicate);actions[duplicate]={'action':'approved_duplicate_not_counted','reason':'operator_approved_duplicate','keep_record_id':keep}
        active={rid:row for rid,row in primary.items() if rid not in suppressed}
        txn_keys={}
        for rid,row in active.items():
            key=(row['account_id'],row['transaction_id'])
            if key in txn_keys:issue('UNRESOLVED_DUPLICATE_TRANSACTION',rid)
            else:txn_keys[key]=rid
        links=decisions.get('links',[]);require(isinstance(links,list),'LINK_DECISIONS_INVALID')
        linked_transactions=set()
        for link in links:
            require(isinstance(link,dict) and identifier(link.get('evidence_ref')),'LINK_APPROVAL_REQUIRED')
            pid=link.get('primary_record_id');refs=link.get('corroborating_record_ids');kind=link.get('link_type')
            require(pid in active and isinstance(refs,list) and refs and len(refs)==len(set(refs)),'LINK_RECORDS_INVALID')
            require(kind in ('transaction_evidence','document_reference'),'LINK_TYPE_INVALID')
            for ref in refs:
                require(ref in rows and ref not in primary,'LINK_TARGET_NOT_CORROBORATION')
                row=rows[ref]
                if kind=='transaction_evidence':
                    require(row['kind'] in CASH and ref not in linked_transactions,'TRANSACTION_LINK_REUSED_OR_NONCASH')
                    for field in ['account_id','kind','settlement_date','unit','amount']:require(row.get(field)==active[pid].get(field),'TRANSACTION_LINK_VALUE_MISMATCH')
                    linked_transactions.add(ref)
                else:require(row['kind']=='receivable_invoice','DOCUMENT_LINK_NOT_INVOICE')
                actions[ref].setdefault('linked_primary_records',[]).append(pid)
        pairs=unique_id(decisions.get('transfers',[]),'pair_id','TRANSFER_PAIR_ID_INVALID')
        transfer_records=set()
        for pair_id,pair in pairs.items():
            require(identifier(pair.get('evidence_ref')),'TRANSFER_APPROVAL_REQUIRED')
            outgoing=pair.get('out_record_id');incoming=pair.get('in_record_id')
            require(outgoing in active and incoming in active and outgoing!=incoming,'TRANSFER_RECORDS_INVALID')
            require(outgoing not in transfer_records and incoming not in transfer_records,'TRANSFER_RECORD_REUSED')
            send,receive=active[outgoing],active[incoming]
            matched=(send['kind']=='cash_out' and receive['kind']=='cash_in' and send['account_id']!=receive['account_id'] and all(send[k]==receive[k] for k in ['amount','unit','settlement_date']))
            if not matched:issue('TRANSFER_PAIR_NOT_MATCHED',pair_id);continue
            transfer_records.update([outgoing,incoming])
            for rid in [outgoing,incoming]:actions[rid]={'action':'eliminated_internal_transfer','reason':'operator_approved_matching_pair','pair_id':pair_id}
            out['approved_transfer_pairs'].append({'pair_id':pair_id,'out_record_id':outgoing,'in_record_id':incoming,'amount':send['amount'],'unit':'CNY'})
        for rid,row in active.items():
            if row.get('internal_transfer_hint') and rid not in transfer_records:issue('INTERNAL_TRANSFER_NOT_PAIRED',rid)
        selected=[rid for rid in active if rid not in transfer_records]
        for rid in selected:
            if rows[rid].get('category') not in GROUPS:issue('CASH_CATEGORY_UNCONFIRMED',rid)
        out['record_decisions']=[{'record_id':rid,**actions[rid]} for rid in rows]
        out['selected_records']=[dict(rows[rid]) for rid in selected]
        out['counts']={'input_records':len(rows),'primary_actual_records':len(primary),'selected_actual_records':len(selected),'approved_duplicate_records':len(suppressed),'eliminated_transfer_records':len(transfer_records),'corroborating_only_records':sum(v['action']=='corroborating_only' for v in actions.values()),'noncash_records':sum(v['action']=='noncash_excluded' for v in actions.values())}
        require(sum([out['counts'][k] for k in ['selected_actual_records','approved_duplicate_records','eliminated_transfer_records','corroborating_only_records','noncash_records']])==len(rows),'DECISION_COUNTS_DO_NOT_CLOSE')
        for name,blob in raw.items():require(small_file(safe_file(bundle,name))==blob,'INPUT_CHANGED_DURING_SELECTION')
        require(small_file(trust_file)==traw,'TRUST_CHANGED_DURING_SELECTION')
        out['status']='blocked' if out['issues'] else 'ready'
        out['selection_sha256']=sha(canonical({'approved_inputs':approved,'context':ctx,'record_decisions':out['record_decisions']}))
        out['limitations']=['Explicit operator registration is not authenticated source truth.','No approximate matching, guessed classification or automatic write is performed.']
    except BindingError as exc:
        out.update(status='invalid',selected_records=[],record_decisions=[],approved_transfer_pairs=[],counts=None)
        issue(exc.code)
    except (ValueError,TypeError,KeyError,OSError,OverflowError):
        out.update(status='invalid',selected_records=[],record_decisions=[],approved_transfer_pairs=[],counts=None)
        issue('SELECTION_INPUT_INVALID')
    return out
