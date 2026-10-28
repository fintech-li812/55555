from pathlib import Path
import copy,hashlib,json,os,shutil,sys,tempfile,unittest
from datetime import date
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT))
import entry
os.environ.update(entry.environment())
import source_audit as a
from program_workbench import readiness,calculate

class OriginalAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.book=a.read_all()
    def setUp(self):self.book=copy.deepcopy(type(self).book)
    def set(self,sheet,cell,value,typ='n'):
        self.book['blocks'][sheet]['cells'][cell]={'value':value,'raw_xml_value':value,'cell_type':typ,'has_formula':False}
    def audit(self):return a.audit_book(self.book,a.source.SOURCE_SHA256)
    def codes(self):return [i['code'] for i in self.audit()['issues']]
    def test_all_eight_sheets_actually_read(self):
        self.assertEqual(set(self.book['blocks']),set(a.SHAPES));self.assertIn('B28',self.book['blocks']['企业基本信息']['cells'])
        self.assertIn('F17',self.book['blocks']['银行流水']['cells'])
    def test_actual_findings_not_old_dictionary(self):
        self.assertEqual(set(self.codes()),{'operating_net_mismatch','duplicate_rows_preserved','unit_confirmation_required','bank_vs_recorded_collected_difference','negative_financing_amount'})
        self.assertNotIn('invalid_date',self.codes())
    def test_normal_partial_receipts_not_errors(self):
        out=self.audit();self.assertEqual(out['normal_partial_receipt_rows'],[16,22,26])
        self.assertFalse(out['normal_partial_receipts_treated_as_errors'])
        self.assertEqual(len([i for i in out['observations'] if i['code']=='normal_partial_receipt']),3)
    def test_bank_compare_collected_not_invoice_total(self):
        self.set('应收账款','F4','411.39')
        self.assertNotIn('bank_vs_recorded_collected_difference',self.codes())
        self.assertIn(4,self.audit()['normal_partial_receipt_rows'])
    def test_bank_difference_has_both_evidence_values(self):
        i=next(i for i in self.audit()['issues'] if i['code']=='bank_vs_recorded_collected_difference')
        self.assertEqual(i['listed_bank_yuan'],'4113900.00');self.assertEqual(i['recorded_collected_yuan'],'5713800.00')
    def test_original_illegal_date_seed_not_assumed_present(self):
        self.assertEqual(a.parsed_date(self.book,'应收账款','D12').isoformat(),'2026-11-22')
    def test_invalid_date_is_actually_detected(self):
        self.set('应收账款','D12','2026-13-05','inlineStr');self.assertIn('invalid_date',self.codes())
    def test_excel_1900_phantom_leap_day_rejected(self):
        self.set('应收账款','D12','60')
        with self.assertRaises(ValueError):a.parsed_date(self.book,'应收账款','D12')
    def test_excel_1904_day_zero(self):
        self.book['date_system']='1904';self.set('应收账款','D12','0')
        self.assertEqual(a.parsed_date(self.book,'应收账款','D12'),date(1904,1,1))
    def test_source_rounding_to_yuan_not_wanyuan(self):
        self.assertEqual(str(a.yuan(self.book,'应收账款','E17')),'5844400.00')
        self.assertEqual(str(a.yuan(self.book,'应收账款','F22')),'7033100.00')
    def test_boolean_not_money(self):
        self.set('现金流水','D3','1','b');self.assertIn('invalid_amount',self.codes())
    def test_formula_cache_not_executed(self):
        self.book['blocks']['现金流水']['cells']['D3']['has_formula']=True
        self.assertIn('invalid_amount',self.codes())
    def test_missing_amount_not_zero(self):
        self.set('现金流水','D3',None)
        out=self.audit();self.assertIsNone(out['monthly_operating_preview'][0]['inflow_yuan'])
        self.assertIn('invalid_amount',[i['code'] for i in out['issues']])
    def test_explicit_zero_is_preserved(self):
        self.set('现金流水','D3','0')
        self.assertEqual(self.audit()['monthly_operating_preview'][0]['inflow_yuan'],'0.00')
    def test_header_contract_blocks_conversion(self):
        self.set('现金流水','D2','经营性流入（元）','inlineStr')
        out=self.audit();self.assertEqual(out['status'],'blocked');self.assertEqual(out['monthly_operating_preview'],[])
    def test_month_gap_detected(self):
        self.set('现金流水','A7','2026-01','inlineStr');self.assertIn('monthly_gap',self.codes())
    def test_missing_first_month_not_silently_redefined(self):
        for col in 'ABCDEFGHI':self.book['blocks']['现金流水']['cells'].pop(col+'3',None)
        self.assertIn('declared_month_coverage_mismatch',self.codes())
    def test_no_duplicate_deletion_or_total(self):
        out=self.audit();self.assertEqual(out['sheet_data_row_counts']['现金流水'],13)
        self.assertFalse(out['cashflow_aggregation_performed']);self.assertFalse(out['source_modified'])
    def test_unknown_unit_threshold_not_invented(self):
        check=next(c for c in self.audit()['checks'] if c['code']=='statistical_unit_outlier')
        self.assertEqual(check['status'],'not_executed_policy_pending')
    def test_future_loan_maturity_is_not_invalid(self):
        self.assertFalse(any(i['code']=='invalid_date' and i['source'].startswith('筹资信息') for i in self.audit()['issues']))
    def test_mapping_no_double_count_or_credit_as_cash(self):
        rows=self.audit()['mapping_preview'];self.assertEqual(len(rows),46)
        self.assertTrue(all(r['counted'] is False for r in rows))
        credit=next(r for r in rows if r['sheet']=='筹资信息' and r['row']==3)
        self.assertIn('不是实际收到',credit['reason'])
    def test_original_source_remains_blocked_without_other_demo(self):
        out=calculate('manufacturing_sample_v1');self.assertEqual(out['status'],'blocked');self.assertIsNone(out['result'])
    def test_readiness_separates_executed_and_pending(self):
        out=readiness();self.assertTrue(out['gate_rules_executed']);self.assertFalse(out['full_data_gate_executed'])
        self.assertTrue(out['account_binding']['module_connected']);self.assertTrue(out['cash_selection']['module_connected'])
        self.assertIn('mapping_review',out['issue_groups']);self.assertFalse(out['can_claim_full_cashflow'])
    def test_existing_program_algorithm_unchanged(self):
        from program_engine import calculate_source
        self.assertEqual(calculate_source()['result']['execution']['algorithm_sha256'],'17d3cfec5bc4051c4448010f644833eb93c033c519e86028d05c02daac1d266b')
    def test_original_file_unchanged(self):
        self.assertEqual(hashlib.sha256(a.source.SOURCE_PATH.read_bytes()).hexdigest(),a.source.SOURCE_SHA256)
    def test_unknown_source_rejected(self):
        with self.assertRaises(ValueError):a.audit_source('../../private')

class BindingAdapterTests(unittest.TestCase):
    def setUp(self):
        tempbase=ROOT/'.tmp';tempbase.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='binding-check-',dir=tempbase)
        self.root=Path(self.tmp.name);self.assertTrue(self.root.resolve().is_relative_to(tempbase.resolve()))
        target=self.root/'bindings';target.mkdir()
        fixtures=ROOT/'tests/fixtures/account_binding'
        shutil.copytree(fixtures/'complete_bundle',target/'bundle')
        (target/'trust.json').write_bytes((fixtures/'trusted_binding.json').read_bytes())
        trust=json.loads((target/'trust.json').read_text(encoding='utf-8'))
        self.record={'state':'registered','source_sha256':a.source.SOURCE_SHA256,'bundle':'bundle','trust_file':'trust.json',
            'trust_sha256':hashlib.sha256((target/'trust.json').read_bytes()).hexdigest(),
            'allow_fixture':True,'context':{k:trust[k] for k in ('tenant_id','company_id','period','source_revision','run_id')}}
        self.write()
    def tearDown(self):
        self.assertTrue(self.root.resolve().is_relative_to((ROOT/'.tmp').resolve()));self.tmp.cleanup()
    def write(self):(self.root/'bindings/registry.json').write_text(json.dumps({'manufacturing_sample_v1':self.record}),encoding='utf-8')
    def check(self):
        with patch.object(a,'ROOT',self.root):return a.account_binding_status()
    def test_reused_module_registered_evidence_ready(self):
        out=self.check();self.assertEqual(out['status'],'ready');self.assertTrue(out['module_connected']);self.assertTrue(out['fixture'])
    def test_registered_trust_digest_required(self):
        self.record['trust_sha256']='0'*64;self.write()
        with self.assertRaises(ValueError):self.check()
    def test_uploaded_source_cannot_self_authorize_binding(self):
        self.record['source_sha256']='0'*64;self.write()
        with self.assertRaises(ValueError):self.check()
    def test_binding_cannot_escape_registration_root(self):
        self.record['trust_file']='../trust.json';self.write()
        with self.assertRaises(ValueError):self.check()
    def test_bound_context_must_match_registration(self):
        self.record['context']['company_id']='different';self.write()
        with self.assertRaises(ValueError):self.check()
    def test_unregistered_missing_values_not_filled_zero(self):
        out=a.account_binding_status();self.assertFalse(out['balance_binding_ready']);self.assertNotIn('balance_totals',out)
    def test_cash_source_selection_requires_accounts(self):
        out=a.cash_selection_status('manufacturing_sample_v1',{'status':'blocked'})
        self.assertTrue(out['module_connected']);self.assertIsNone(out['cashflow_totals'])

if __name__=='__main__':unittest.main()
