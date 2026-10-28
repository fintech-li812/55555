"""One-time, exclusive creation of explicitly synthetic demonstration source/evidence."""
from pathlib import Path
from xml.sax.saxutils import escape
import hashlib,json,sys,zipfile
ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'src'))
import eight_sheet_engine as e
import account_binding as b
import cash_selection as cs

def write_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('x',encoding='utf-8') as f:json.dump(data,f,ensure_ascii=False,indent=2)

def make_workbook(path,tables):
    need=not path.exists();assert need,'Refuse to overwrite workbook'
    parts={};ns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'
    names=list(tables)
    for i,(name,rows) in enumerate(tables.items(),1):
        nodes=[]
        for r,row in enumerate(rows,1):
            cells=[]
            for j,value in enumerate(row,1):
                col=chr(64+j);ref=col+str(r);style='1' if r<=2 else '0'
                numeric=r>=3 and ((name=='现金流水' and col in 'DEFG') or (name=='应收账款' and col in 'EF') or
                    (name=='银行流水' and col=='C') or (name=='筹资信息' and col=='C') or (name=='投资现金流' and col=='D'))
                if numeric and value!='':cells.append(f'<c r="{ref}" s="{style}"><v>{escape(str(value))}</v></c>')
                else:cells.append(f'<c r="{ref}" s="{style}" t="inlineStr"><is><t>{escape(str(value))}</t></is></c>')
            nodes.append(f'<row r="{r}">'+''.join(cells)+'</row>')
        last=chr(64+e.SHAPE[name]);cols=''.join(f'<col min="{j}" max="{j}" width="{48 if j==1 else 26}" customWidth="1"/>' for j in range(1,e.SHAPE[name]+1))
        parts[f'xl/worksheets/sheet{i}.xml']=f'<worksheet xmlns="{ns}"><sheetViews><sheetView workbookViewId="0"><pane ySplit="2" topLeftCell="A3" state="frozen"/></sheetView></sheetViews><cols>{cols}</cols><sheetData>'+''.join(nodes)+f'</sheetData><mergeCells count="1"><mergeCell ref="A1:{last}1"/></mergeCells></worksheet>'
    parts['xl/workbook.xml']=f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join(f'<sheet name="{escape(n)}" sheetId="{i}" r:id="rId{i}"/>' for i,n in enumerate(names,1))+'</sheets></workbook>'
    relns='http://schemas.openxmlformats.org/package/2006/relationships';typens='http://schemas.openxmlformats.org/officeDocument/2006/relationships/'
    parts['xl/_rels/workbook.xml.rels']=f'<Relationships xmlns="{relns}">'+''.join(f'<Relationship Id="rId{i}" Type="{typens}worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,9))+f'<Relationship Id="rId9" Type="{typens}styles" Target="styles.xml"/></Relationships>'
    parts['_rels/.rels']=f'<Relationships xmlns="{relns}"><Relationship Id="rId1" Type="{typens}officeDocument" Target="xl/workbook.xml"/></Relationships>'
    parts['[Content_Types].xml']='<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'+''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,9))+'</Types>'
    parts['xl/styles.xml']=f'<styleSheet xmlns="{ns}"><fonts count="2"><font><sz val="11"/><name val="Arial"/><color rgb="FF164596"/></font><font><b/><sz val="11"/><name val="Arial"/><color rgb="FFFFFFFF"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF18394E"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0"/></cellXfs></styleSheet>'
    with zipfile.ZipFile(path,'x',zipfile.ZIP_DEFLATED) as z:
        for name,text in parts.items():z.writestr(name,('<?xml version="1.0" encoding="UTF-8"?>'+text).encode())

def main():
    assert not e.BASE.exists(),'One-time builder refuses existing evidence directory'
    basic=[['完整八表合成演示｜全部数据为SYNTHETIC，不是真实企业'],['字段','值','说明']]
    info={3:('企业名称','合成制造企业_演示专用'),4:('行业代码','C13'),5:('所属行业','制造业'),
        6:('企业规模','合成演示'),7:('在职人数','20'),8:('营业收入','仅合成，不作为现金'),9:('缓冲','未启用风险模型'),
        10:('本月经营流出（万元）','7'),11:('黄灯阈值','本演示不启用'),12:('红灯阈值','本演示不启用'),
        25:('数据区间','2026-08-01 至 2026-08-31'),26:('提供者','合成构造'),27:('数据版本','SYNTHETIC eight-sheet v1'),28:('使用边界','完整工程联调，不是真实银行/企业证据')}
    for r in range(3,29):
        name,value=info.get(r,(f'保留字段{r}','本期不使用'))
        basic.append([name,value,'人工构造的输入；结果由程序计算；非真实企业'])
    tables={'企业基本信息':basic,
        '现金流水':[['合成月度经营汇总；期末列是独立合成余额的展示值'],['月份','月末日期','流水类型','经营性流入（万元）','经营性流出（万元）','净现金流（万元）','期末现金余额（万元）','说明','数据闸结果'],['2026-08','2026-08-31','经营性','13','7','6','17.7','独立构造汇总，用于与银行实收付核对','由程序检查；非静态结论']],
        '应收账款':[['合成应收：正常部分到账，不能把20万元发票叠加现金'],['单据编号','客户名称','开票日期','到期日','应收金额（万元）','已收金额（万元）','实际收款日期','到账状态','备注','数据闸结果'],['SYN-AR-001','合成客户A','2026-08-01','2026-08-31','20','13','2026-08-05','部分到账','余款7万元未收，属正常业务','由程序检查']],
        '筹资信息':[['合成融资：额度/余额排除，仅实际收还款关联银行'],e.FIN_HEADERS,
            ['银行授信额度','合成银行','10','非现金，不叠加','2027-08-31','','','',''],
            ['贷款余额','合成银行','3.2','存量，不叠加','2027-08-31','','','',''],
            ['实际借款收款','合成银行','5','已到账；合成','2027-08-31','2026-08-25','inflow','demo-bank-001','SYN-T5'],
            ['实际偿还本金','合成银行','1.8','已支付；合成','2027-08-31','2026-08-26','outflow','demo-bank-001','SYN-T6']],
        '投资现金流':[['合成投资：支出为负，收款为正；关联银行，不重复计现'],e.INV_HEADERS,
            ['合成设备购置','资本支出','2026-08-12','-2','已支付','合成凭证','demo-bank-001','SYN-T3'],
            ['合成旧设备处置','资产处置','2026-08-20','0.5','已收款','合成凭证','demo-bank-001','SYN-T4']],
        '银行流水':[['合成完整账户流水；仅此表为实际收付主来源'],e.BANK_HEADERS],
        '行业参数':[['合成演示不启用评级或风险阈值'],['行业','行业代码','说明','保留4','保留5','保留6','保留7','保留8','保留9','保留10','保留11','保留12','保留13'],['制造业','C13','现金流基础核对','','','','','','','','','','']],
        '数据字典':[['SYNTHETIC完整八表演示｜无故意埋错'],['编号','所在Sheet','位置','说明','处理规则','用途'],
            ['SYN-01','全表','全部','人工构造，不是真实企业或银行资料','仅合成工程演示','醒目标识'],
            ['SYN-02','银行流水','3:8','6笔主收付，其他表只佐证','不重复计现','可追溯计算'],
            ['SYN-03','独立余额文件','期初/期末','分别明确构造100000及177000元，不由运行时计算回填','按登记文件核对','余额差异测试'],
            ['SYN-04','全部输入','编辑说明','输入表只含静态测试输入，计算结果由程序返回','改动后须另版登记，不自动更新摘要','保护历史结果']]}
    tx=[('05','合成客户A','13','货款','SYN-AR-001','inflow','operating','SYN-T1'),
        ('10','合成供应商','7','材料付款','','outflow','operating','SYN-T2'),
        ('12','合成设备商','2','购置设备','','outflow','investing','SYN-T3'),
        ('20','合成设备买方','0.5','设备处置','','inflow','investing','SYN-T4'),
        ('25','合成银行','5','实际借款','','inflow','financing','SYN-T5'),
        ('26','合成银行','1.8','偿还本金','','outflow','financing','SYN-T6')]
    for day,party,amount,purpose,invoice,direction,category,txn in tx:
        tables['银行流水'].append(['2026-08-'+day,party,amount,purpose,invoice,'SYNTHETIC',direction,'demo-bank-001',category,txn,'primary_actual','false'])
    e.WORKBOOK.parent.mkdir(exist_ok=True);make_workbook(e.WORKBOOK,tables)
    context=dict(tenant_id='fixture-tenant',company_id='fixture-manufacturing',period='2026-08',source_revision='synthetic-eight-evidence-v1',run_id='synthetic-eight-binding-v1')
    common={**context,'origin':'offline_fixture'};e.BASE.mkdir()
    write_json(e.BASE/'account/context.json',{**context,'industry':'manufacturing'})
    write_json(e.BASE/'account/scope.json',dict(schema_version='mfg-account-scope.v1',**common,unit='CNY',scope_confirmed=True,
        reporting_entity_id='synthetic-entity',opening_as_of='2026-07-31',closing_as_of='2026-08-31',
        accounts=[dict(account_id='demo-bank-001',reporting_entity_id='synthetic-entity',currency='CNY',included=True)]))
    observations=[];sources=[]
    for kind,amount,asof in [('opening','100000.00','2026-07-31'),('closing','177000.00','2026-08-31')]:
        record=dict(source_record_id=kind,account_id='demo-bank-001',kind=kind,amount=amount,unit='CNY',as_of=asof,source_location='SYNTHETIC-'+kind+'-explicit-input')
        path='sources/'+kind+'.json'
        write_json(e.BASE/'account'/path,dict(schema_version='mfg-balance-source-records.v1',**common,source_class='bank_statement_extract',derivation='registered_independent_actual',data_type='SYNTHETIC',records=[record]))
        sources.append(dict(evidence_id='synthetic-'+kind,path=path,sha256=e.sha((e.BASE/'account'/path).read_bytes()),
            source_class='bank_statement_extract',independence='operator_approved_independent_actual',approval_ref='SYNTHETIC-CONSTRUCTION-NOT-REAL-BANK-APPROVAL'))
        observations.append({k:v for k,v in record.items() if k!='source_location'}|dict(record_id='balance-'+kind,evidence_id='synthetic-'+kind))
    write_json(e.BASE/'account/balances.json',dict(schema_version='mfg-balance-observations.v1',**common,unit='CNY',records=observations))
    write_json(e.BASE/'account/evidence_manifest.json',dict(schema_version='mfg-balance-evidence-manifest.v1',**common,sources=sources))
    write_json(e.BASE/'account_trust.json',dict(schema_version='mfg-account-binding-trust.v1',**context,industry='manufacturing',
        approval_ref='SYNTHETIC-DEMONSTRATION-USER-CHOSE-A',allow_fixture=True,
        approved_inputs={n:e.sha((e.BASE/'account'/n).read_bytes()) for n in b.FILES}))
    bound=b.assess(e.BASE/'account',e.BASE/'account_trust.json',allow_fixture=True)
    assert bound['status']=='ready',bound
    raw=e.WORKBOOK.read_bytes();book=e.read_workbook(raw,e.SHAPE)
    events,decisions,_,_=e.derive_events(book,e.sha(raw),context)
    write_json(e.BASE/'cash/scope_binding.json',bound);write_json(e.BASE/'cash/events.json',events);write_json(e.BASE/'cash/decisions.json',decisions)
    registered=[]
    for sid,role in [('bank-primary','primary_actual'),('business-corroboration','corroborating'),('business-docs','noncash')]:
        item=dict(source_id=sid,role=role,account_ids=['demo-bank-001'],period='2026-08')
        if role=='primary_actual':item.update(complete=True,declared_zero=False)
        registered.append(item)
    write_json(e.BASE/'cash/source_registry.json',dict(schema_version='mfg-cash-source-registry.v1',**common,sources=registered))
    write_json(e.BASE/'cash_trust.json',dict(schema_version='mfg-cash-selection-trust.v1',**context,industry='manufacturing',
        approval_ref='SYNTHETIC-DEMONSTRATION-USER-CHOSE-A',allow_fixture=True,
        approved_inputs={n:e.sha((e.BASE/'cash'/n).read_bytes()) for n in cs.FILES}))
    chosen=cs.plan(e.BASE/'cash',e.BASE/'cash_trust.json',allow_fixture=True);assert chosen['status']=='ready',chosen
    approved={p.relative_to(e.BASE).as_posix():e.sha(p.read_bytes()) for p in e.BASE.rglob('*.json')}
    write_json(e.BASE/'registration.json',dict(schema_version='mfg-synthetic-eight-registration.v1',source_id=e.SOURCE_ID,
        data_type='SYNTHETIC',context=context,workbook_sha256=e.sha(raw),approved_files=approved))
    output=e.calculate();assert output['result']['amounts']['closing']=='177000.00' and output['can_save_source_result']
    print(json.dumps(dict(workbook=str(e.WORKBOOK),source_id=e.SOURCE_ID,amounts=output['result']['amounts'],groups=output['result']['cashflow_groups'],counts=chosen['counts']),ensure_ascii=False))

if __name__=='__main__':main()
