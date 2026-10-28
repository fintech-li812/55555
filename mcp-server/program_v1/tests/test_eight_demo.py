from pathlib import Path
from unittest.mock import patch
import copy,json,os,shutil,sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'src'))
import entry
os.environ.update(entry.environment())
import eight_sheet_engine as e
from program_workbench import calculate,readiness,list_sources,inspect_source,data_audit
from program_service import ProgramService
from service import canonical
from config import Config

class CompleteDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.input_parts=e.load_inputs();cls.output=e.calculate()
    def parts(self):return copy.deepcopy(type(self).input_parts)
    def test_all_three_groups_and_independent_balance(self):
        out=type(self).output;r=out['result']
        self.assertEqual(r['amounts'],dict(opening='100000.00',inflow='185000.00',outflow='108000.00',net='77000.00',closing='177000.00',actual_closing='177000.00',difference='0.00'))
        self.assertEqual(r['cashflow_groups']['operating']['net'],'60000.00')
        self.assertEqual(r['cashflow_groups']['investing']['net'],'-15000.00')
        self.assertEqual(r['cashflow_groups']['financing']['net'],'32000.00')
    def test_only_six_primary_cash_records_count(self):
        c=type(self).output['result']['evidence']['selection_counts']
        self.assertEqual((c['input_records'],c['selected_actual_records'],c['corroborating_only_records'],c['noncash_records']),(13,6,4,3))
    def test_old_demo_stays_distinct(self):
        self.assertEqual(calculate('program_demo_v1')['result']['amounts']['closing'],'140000.00')
        self.assertEqual(calculate(e.SOURCE_ID)['result']['amounts']['closing'],'177000.00')
    def test_original_sample_stays_blocked(self):self.assertEqual(calculate('manufacturing_sample_v1')['status'],'blocked')
    def test_catalog_four_registered_sources(self):
        entries=list_sources()['sources'];self.assertEqual(len(entries),4)
        self.assertEqual(next(s for s in entries if s['source_id']==e.SOURCE_ID)['status'],'available')
    def test_ready_only_synthetic_contract(self):
        out=readiness(e.SOURCE_ID);self.assertEqual(out['status'],'ready');self.assertTrue(out['can_save_source_result'])
        self.assertEqual(out['data_type'],'SYNTHETIC');self.assertFalse(out['full_data_gate_executed'])
    def test_audit_and_full_sheet_inspection(self):
        audit=data_audit(e.SOURCE_ID);self.assertEqual(len(audit['sheet_names']),8)
        self.assertEqual(len(audit['checks']),13);self.assertEqual(audit['issues'],[])
        self.assertIn('L8',inspect_source(e.SOURCE_ID)['blocks']['银行流水']['cells'])
    def test_partial_invoice_is_normal_and_not_cash_again(self):
        obs=type(self).output['observations'];self.assertEqual(len([o for o in obs if o['code']=='normal_partial_receipt']),1)
        self.assertEqual(type(self).output['result']['amounts']['inflow'],'185000.00')
    def test_source_aware_save_approval(self):
        service=ProgramService(Config.from_env());r=type(self).output['result']
        self.assertEqual(canonical(service.approved_result(r)),canonical(r))
    def test_valid_complete_result_reaches_db_boundary_only(self):
        service=ProgramService(Config.from_env())
        with patch.object(service,'connect',side_effect=RuntimeError('verified-db-boundary')):
            with self.assertRaisesRegex(RuntimeError,'verified-db-boundary'):service.save(type(self).output['result'],'unit-no-write')
    def test_tampered_source_cannot_use_other_demo(self):
        service=ProgramService(Config.from_env());r=service.read()['result'];r['source']['workbook_ref']=e.SOURCE_ID
        with patch.object(service,'connect',side_effect=AssertionError('no_db')):
            with self.assertRaisesRegex(ValueError,'differs_from_approved'):service.save(r,'tamper-no-write')
    def test_edited_result_rejected_before_db(self):
        service=ProgramService(Config.from_env());r=copy.deepcopy(type(self).output['result']);r['evidence']['selected_actual_record_count']=999
        with patch.object(service,'connect',side_effect=AssertionError('no_db')):
            with self.assertRaisesRegex(ValueError,'differs_from_approved'):service.save(r,'tamper-no-write')
    def test_read_source_not_cached_small_fixture(self):
        r=ProgramService(Config.from_env()).read_source(e.SOURCE_ID)
        self.assertEqual(r['result']['source']['workbook_ref'],e.SOURCE_ID)
        self.assertEqual(r['result']['amounts']['closing'],'177000.00')
    def test_deterministic_replay(self):self.assertEqual(e.calculate(),type(self).output)
    def test_modified_primary_amount_changes_calculation(self):
        reg,book,bound,chosen,checks,obs=self.parts()
        chosen['selected_records'][0]['amount']='130000.01'
        r=e.compute(reg,book,bound,chosen,checks,obs)
        self.assertEqual(r['result']['amounts']['closing'],'177000.01')
        self.assertEqual(r['result']['amounts']['difference'],'-0.01');self.assertFalse(r['can_save_source_result'])
    def test_independent_balance_not_auto_filled(self):
        reg,book,bound,chosen,checks,obs=self.parts()
        bound['balance_totals']['independent_closing']='176000.00'
        book['blocks']['现金流水']['cells']['G3']['value']='17.6'
        out=e.compute(reg,book,bound,chosen,checks,obs)
        self.assertEqual(out['result']['amounts']['difference'],'-1000.00');self.assertFalse(out['can_save_source_result'])
    def test_sheet_balance_and_independent_statement_must_match(self):
        reg,book,bound,chosen,checks,obs=self.parts();book['blocks']['现金流水']['cells']['G3']['value']='999'
        with self.assertRaisesRegex(ValueError,'stated_and_independent'):e.compute(reg,book,bound,chosen,checks,obs)
    def test_negative_closing_is_not_green(self):
        reg,book,bound,chosen,checks,obs=self.parts();bound['balance_totals'].update(opening='-180000.00',independent_closing='-103000.00')
        book['blocks']['现金流水']['cells']['G3']['value']='-10.3'
        r=e.compute(reg,book,bound,chosen,checks,obs)['result'];self.assertIn('negative_closing_cash',r['risk_flags'])
    def test_duplicate_bank_transaction_rejected(self):
        reg,book,*_=self.parts();book['blocks']['银行流水']['cells']['J4']['value']='SYN-T1'
        with self.assertRaisesRegex(ValueError,'duplicate_or_missing_bank'):e.derive_events(book,reg['workbook_sha256'],reg['context'])
    def test_completed_disposal_does_not_imply_receipt(self):
        reg,book,*_=self.parts();book['blocks']['投资现金流']['cells']['E4']['value']='已完成'
        with self.assertRaisesRegex(ValueError,'settlement_not_explicit'):e.derive_events(book,reg['workbook_sha256'],reg['context'])
    def test_credit_limit_not_allowed_cash_fields(self):
        reg,book,*_=self.parts();book['blocks']['筹资信息']['cells']['F3']['value']='2026-08-01'
        with self.assertRaisesRegex(ValueError,'noncash_financing_has_cash'):e.derive_events(book,reg['workbook_sha256'],reg['context'])
    def test_units_not_guessed(self):
        reg,book,*_=self.parts();book['blocks']['银行流水']['cells']['C2']['value']='金额（元）'
        with self.assertRaisesRegex(ValueError,'header_mismatch'):e.derive_events(book,reg['workbook_sha256'],reg['context'])

class RegisteredEvidenceTests(unittest.TestCase):
    def setUp(self):
        parent=ROOT/'.tmp';parent.mkdir(exist_ok=True);self.tmp=tempfile.TemporaryDirectory(prefix='eight-input-',dir=parent)
        self.root=Path(self.tmp.name);self.assertTrue(self.root.resolve().is_relative_to(parent.resolve()))
        shutil.copytree(e.BASE,self.root/'demo_eight');self.book=self.root/'demo.xlsx';self.book.write_bytes(e.WORKBOOK.read_bytes())
    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to((ROOT/'.tmp').resolve()));self.tmp.cleanup()
    def call(self):
        with patch.object(e,'BASE',self.root/'demo_eight'),patch.object(e,'WORKBOOK',self.book):return e.calculate()
    def test_registered_inputs_can_reproduce(self):self.assertEqual(self.call()['result']['amounts']['closing'],'177000.00')
    def test_workbook_changes_block_before_calculation(self):
        self.book.write_bytes(self.book.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError,'workbook_changed'):self.call()
    def test_closing_source_tampering_detected(self):
        p=self.root/'demo_eight/account/sources/closing.json';p.write_text('{}',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'evidence_file_changed'):self.call()
    def test_no_omitted_evidence_hash(self):
        p=self.root/'demo_eight/registration.json';r=json.loads(p.read_text(encoding='utf-8'));del r['approved_files']['account/sources/closing.json'];p.write_text(json.dumps(r),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'complete_evidence_bindings'):self.call()
    def test_cannot_relabel_real_company(self):
        p=self.root/'demo_eight/registration.json';r=json.loads(p.read_text(encoding='utf-8'));r['data_type']='REAL_PRIVATE';p.write_text(json.dumps(r),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'synthetic_registration'):self.call()
    def test_registration_schema_required(self):
        p=self.root/'demo_eight/registration.json';r=json.loads(p.read_text(encoding='utf-8'));r['schema_version']='other';p.write_text(json.dumps(r),encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'schema_invalid'):self.call()

if __name__=='__main__':unittest.main()
