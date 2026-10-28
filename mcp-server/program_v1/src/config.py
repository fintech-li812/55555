"""Configuration is local trusted scope, not caller-supplied authentication."""
from dataclasses import dataclass,field
from pathlib import Path
import os,re

@dataclass(frozen=True)
class Config:
    tenant: str
    company: str
    result_file: Path
    result_sha256: str
    database: str
    host: str='127.0.0.1'
    port: int=3306
    user: str='cf_app'
    password: str=field(default='',repr=False)
    qa: bool=False
    allow_fixture: bool=False
    qwen_bound: bool=False

    def validate(self):
        if type(self.port) is not int or not 1<=self.port<=65535:
            raise ValueError("database_port_invalid")
        if any(type(flag) is not bool for flag in (self.qa,self.allow_fixture,self.qwen_bound)):
            raise ValueError("configuration_flags_must_be_boolean")
        for value in (self.tenant,self.company):
            if not isinstance(value,str) or not value.strip() or len(value)>128:
                raise ValueError('trusted_scope_missing')
        if not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',self.database):
            raise ValueError('database_name_invalid')
        if self.user=='root' and not (self.qa and self.database.startswith('cfqa_')):
            raise ValueError('root_only_allowed_in_isolated_qa')
        if self.allow_fixture and not (self.qa and self.database.startswith('cfqa_')):
            raise ValueError('fixture_only_allowed_in_isolated_qa')
        if self.host not in ('127.0.0.1','localhost'):
            raise ValueError('v1_requires_loopback_or_ssh_tunnel')
        if not re.fullmatch(r'[0-9a-f]{64}',self.result_sha256):
            raise ValueError('trusted_result_sha256_missing')
        return self

    @classmethod
    def from_env(cls):
        required=['CF_TENANT_ID','CF_COMPANY_ID','CF_RESULT_FILE','CF_RESULT_SHA256','CF_DB_NAME']
        if any(not os.environ.get(k) for k in required):
            raise ValueError('configuration_incomplete')
        return cls(os.environ['CF_TENANT_ID'],os.environ['CF_COMPANY_ID'],Path(os.environ['CF_RESULT_FILE']),
                   os.environ['CF_RESULT_SHA256'],os.environ['CF_DB_NAME'],
                   host=os.getenv('CF_DB_HOST','127.0.0.1'),port=int(os.getenv('CF_DB_PORT','3306')),
                   user=os.getenv('CF_DB_USER','cf_app'),password=os.getenv('CF_DB_PASSWORD',''),
                   qa=os.getenv('CF_QA_MODE')=='1',allow_fixture=os.getenv('CF_ALLOW_FIXTURE')=='1',
                   qwen_bound=os.getenv('CF_QWEN_BOUND')=='1').validate()
