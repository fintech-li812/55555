"""Isolated program candidate. Uses existing approved QA environment, never production."""
from pathlib import Path
import hashlib,json,os,re,sys
ROOT=Path(__file__).resolve().parent
APPROVED_SHA256='e6470f7adeba00804b178be5dc17a3087a88758178b9482cfd64b8cf81cf36d7'

def environment():
    qa=json.loads((ROOT/'qa_binding.json').read_text(encoding='utf-8'))
    if qa.get('port')!=13317 or not isinstance(qa.get('database'),str) or not re.fullmatch(r'cfqa_[a-z0-9_]{1,58}',qa['database']):
        raise ValueError('existing_isolated_qa_scope_required')
    snapshot=ROOT/'fixtures/calculated_result_v1.json'
    if hashlib.sha256(snapshot.read_bytes()).hexdigest()!=APPROVED_SHA256:raise ValueError('historical_fixture_changed')
    env=os.environ.copy()
    env.update(CF_TENANT_ID='fixture-tenant',CF_COMPANY_ID='fixture-manufacturing',
        CF_RESULT_FILE=str(snapshot),CF_RESULT_SHA256=APPROVED_SHA256,
        CF_DB_NAME=qa['database'],CF_DB_HOST='127.0.0.1',CF_DB_PORT='13317',CF_DB_USER='root',
        CF_QA_MODE='1',CF_ALLOW_FIXTURE='1',CF_QWEN_BOUND='0',CF_WEB_PORT='18766',
        CF_RUNTIME_DIR=str(ROOT/'runtime'),PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1')
    env.pop('CF_DB_PASSWORD',None)
    return env

def main():
    # An externally supplied CF_DB_NAME means the caller owns the configuration
    # (deploy/.env). Only fall back to the isolated QA binding when it is absent.
    if not os.environ.get('CF_DB_NAME'):
        os.environ.update(environment())
    sys.path.insert(0,str(ROOT/'src'))
    mode=sys.argv[1] if len(sys.argv)==2 else None
    if mode=='stdio':
        from app import build_mcp
        build_mcp().run(transport='stdio')
    elif mode=='check':
        from program_workbench import project_status
        print(json.dumps(project_status(),ensure_ascii=False,indent=2))
    elif mode=='web':
        from web_server import main as web_main
        web_main()
    elif mode=='http':
        # Remote transport for the server deployment. Binds loopback only; Nginx
        # terminates TLS in front of it. Never expose this port directly.
        from app import build_mcp
        import uvicorn
        host=os.environ.get('MCP_HTTP_HOST','127.0.0.1')
        port=int(os.environ.get('MCP_HTTP_PORT','8000'))
        server=build_mcp()
        # The SDK rejects unknown Host headers (DNS-rebinding guard). Behind Nginx
        # the Host is the public domain, so it has to be declared explicitly.
        allowed=[h.strip() for h in os.environ.get('MCP_ALLOWED_HOSTS','').split(',') if h.strip()]
        if allowed:
            from mcp.server.transport_security import TransportSecuritySettings
            origins=[o.strip() for o in os.environ.get('MCP_ALLOWED_ORIGINS','').split(',') if o.strip()]
            server.settings.transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=allowed,
                allowed_origins=origins or ['https://'+a.split(':')[0] for a in allowed],
            )
        uvicorn.run(server.streamable_http_app(),host=host,port=port,log_level='info')
    else:raise SystemExit('usage: entry.py stdio|check|web|http')

if __name__=='__main__':main()
