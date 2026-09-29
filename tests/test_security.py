import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4
import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import select
from app import main, admin, providers
from app.config import Settings
from app.db import AIProvider, Account, LoginSession, Conversation, Employee, Base
from app.security import passwords, digest
from app.providers import ProviderSession
from test_app import client, payload

KEY='unit-test-secret-key-never-real-123456'


def new_provider(client, **extra):
    return client.post('/api/admin/providers',json={'name':'Test AI','provider':'openai','model':'test-model','api_key':KEY,**extra})


def add_employee(client):
    r=client.post('/api/admin/accounts',json={'username':'staff','employee_id':'STAFF001','name':'員工','department':'人資','password':'staff-password-123','annual_hours':24})
    assert r.status_code==200,r.text
    return r.json()


def login_staff(client):
    r=client.post('/api/auth/login',json={'username':'staff','password':'staff-password-123'})
    assert r.status_code==200,r.text
    client.headers['X-CSRF-Token']=r.json()['csrf']
    return r


def test_private_routes_require_login_and_host(client):
    client.cookies.clear()
    for path in ['/api/dashboard','/api/documents','/api/models','/api/admin/providers','/api/conversations']:
        assert client.get(path).status_code==401
    assert client.get('/api/auth/status').status_code==200
    assert client.get('/api/health',headers={'Host':'evil.example'}).status_code==400


def test_csrf_origin_and_security_headers(client):
    assert client.post('/api/leaves/draft',json=payload(),headers={'X-CSRF-Token':''}).status_code==403
    assert client.post('/api/leaves/draft',json=payload(),headers={'Origin':'https://evil.example'}).status_code==403
    r=client.get('/api/dashboard')
    assert r.headers['Cache-Control']=='no-store'
    assert "frame-ancestors 'none'" in r.headers['Content-Security-Policy']
    assert client.post('/api/chat',content='x'*32769).status_code==413


def test_login_cookie_logout_expiry(client):
    r=client.post('/api/auth/login',json={'username':'admin','password':'test-password-123'})
    assert 'HttpOnly' in r.headers['set-cookie'] and 'SameSite=strict' in r.headers['set-cookie']
    client.headers['X-CSRF-Token']=r.json()['csrf']
    assert client.post('/api/auth/logout').status_code==200
    assert client.get('/api/dashboard').status_code==401
    r=client.post('/api/auth/login',json={'username':'admin','password':'test-password-123'})
    with main.SessionLocal.begin() as db:
        db.get(LoginSession,digest(client.cookies.get('daywork_session'))).expires_at=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(seconds=1)
    assert client.get('/api/dashboard').status_code==401


def test_cross_employee_and_admin_isolation(client):
    chat=client.post('/api/chat',json={'message':'我還剩多少特休？'}).json()
    leave=client.post('/api/leaves/draft',json=payload()).json()
    add_employee(client);login_staff(client)
    assert client.get('/api/dashboard').json()['employee']['id']=='STAFF001'
    assert client.get('/api/conversations').json()==[]
    assert client.get('/api/conversations/'+chat['conversation_id']).status_code==404
    assert client.post('/api/chat',json={'message':'繼續','conversation_id':chat['conversation_id']}).status_code==404
    assert client.post('/api/leaves/'+leave['id']+'/confirm').status_code==404
    assert client.get('/api/leaves/'+leave['id']+'/events').status_code==404
    assert client.get('/api/admin/providers').status_code==403
    assert new_provider(client).status_code==403
    assert client.post('/api/admin/accounts',json={'username':'fake'}).status_code==403


def test_disabled_account_and_password_change_revoke_sessions(client):
    staff=add_employee(client)
    token=client.cookies.get('daywork_session')
    csrf=client.headers['X-CSRF-Token']
    login_staff(client);staff_token=client.cookies.get('daywork_session')
    client.cookies.clear();client.cookies.set('daywork_session',token);client.headers['X-CSRF-Token']=csrf
    assert client.patch('/api/admin/accounts/'+staff['id'],json={'disabled':True}).status_code==200
    client.cookies.clear();client.cookies.set('daywork_session',staff_token)
    assert client.get('/api/dashboard').status_code==401
    client.cookies.clear();client.cookies.set('daywork_session',token)
    assert client.post('/api/auth/password',json={'current_password':'test-password-123','new_password':'new-password-1234'}).status_code==200
    assert client.get('/api/dashboard').status_code==401
    assert client.post('/api/auth/login',json={'username':'admin','password':'test-password-123'}).status_code==401
    assert client.post('/api/auth/login',json={'username':'admin','password':'new-password-1234'}).status_code==200


def test_setup_cannot_run_twice_and_login_throttled(client):
    assert client.get('/api/auth/status').json()['setup_required'] is False
    assert client.post('/api/auth/setup',json={'username':'hacker','password':'whatever-test-123','name':'H','token':'x'*32}).status_code==409
    for _ in range(10):
        assert client.post('/api/auth/login',json={'username':'nonexistent','password':'wrong'}).status_code==401
    assert client.post('/api/auth/login',json={'username':'nonexistent','password':'wrong'}).status_code==429


def test_vault_redaction_validation_and_mask(client):
    r=new_provider(client);assert r.status_code==200,r.text
    assert KEY not in r.text and r.json()['key_mask'].endswith('3456')
    for url in ['/api/admin/providers','/api/models','/api/admin/events']:
        assert KEY not in client.get(url).text
    with main.SessionLocal() as db:
        row=db.get(AIProvider,r.json()['id'])
        assert KEY not in row.encrypted_key
        assert main.app.state.vault.decrypt(row.encrypted_key.encode()).decode()==KEY
    bad=new_provider(client,model='bad model',api_key=KEY)
    assert bad.status_code==422 and KEY not in bad.text and 'input' not in bad.text
    assert new_provider(client,provider='custom',api_key=KEY).status_code==422
    assert new_provider(client,base_url='http://127.0.0.1').status_code==422


def test_provider_lifecycle_rotation_and_employee_catalog(client,monkeypatch):
    pid=new_provider(client).json()['id']
    url='/api/admin/providers/'+pid
    assert client.post(url+'/activate',json={'enabled':True}).status_code==409
    monkeypatch.setattr(admin,'probe',lambda *args:None)
    assert client.post(url+'/test').status_code==200
    assert client.post(url+'/activate',json={'enabled':True,'make_default':True}).json()['default']
    catalog=client.get('/api/models').json()
    assert len(catalog)==1 and 'key_mask' not in catalog[0] and 'encrypted_key' not in catalog[0]
    assert client.delete(url).status_code==409
    update=client.put(url,json={'name':'Updated','provider':'gemini','model':'new-model','api_key':'rotated-secret-123456'})
    assert not update.json()['tested'] and not update.json()['enabled']
    assert client.get('/api/models').json()==[]
    assert client.delete(url).status_code==200


def test_provider_failure_redacts_upstream_and_race_rejects(client,monkeypatch):
    pid=new_provider(client).json()['id'];url='/api/admin/providers/'+pid
    def failed(*args):raise RuntimeError('provider response includes '+KEY)
    monkeypatch.setattr(admin,'probe',failed)
    result=client.post(url+'/test');assert result.status_code==502 and KEY not in result.text
    def concurrent_rotation(*args):
        with main.SessionLocal.begin() as db:db.get(AIProvider,pid).revision+=1
    monkeypatch.setattr(admin,'probe',concurrent_rotation)
    assert client.post(url+'/test').status_code==409


def test_configured_chat_uses_chosen_provider_and_tools(client,monkeypatch):
    pid=new_provider(client,provider='anthropic').json()['id'];seen=[]
    with main.SessionLocal.begin() as db:
        row=db.get(AIProvider,pid);row.enabled=True;row.tested=True
    class Fake:
        def __init__(self,*args):seen.append(args[:3]);self.n=0
        def step(self):
            self.n+=1
            return ('', [{'id':'call1','name':'get_leave_balance','arguments':'{}'}]) if self.n==1 else ('特休 36 小時',[])
        def results(self,results):assert results[0][1]['balances'][0]['hours']==36
        def close(self):seen.append('closed')
    monkeypatch.setattr(providers,'ProviderSession',Fake)
    r=client.post('/api/chat',json={'message':'查餘額','provider_id':pid})
    assert r.status_code==200,r.text
    assert r.json()['mode']=='anthropic' and len(r.json()['trace'])==1
    assert seen[0]==('anthropic','test-model',KEY) and seen[-1]=='closed'
    assert KEY not in r.text
    assert client.post('/api/chat',json={'message':'你好','provider_id':str(uuid4())}).status_code==409


def test_no_silent_demo_fallback_in_production(client,monkeypatch):
    monkeypatch.setattr(main.settings,'environment','production')
    r=client.post('/api/chat',json={'message':'你好'})
    assert r.status_code==503 and '尚未設定' in r.text


def test_production_configuration_fails_closed(tmp_path):
    base={'environment':'production','public_origin':'https://admin.example.com','encryption_key':Fernet.generate_key().decode(),'seed_demo':False,'database_url':'postgresql+psycopg://a:b@localhost/db','qdrant_url':'http://localhost:6333','knowledge_dir':str(tmp_path)}
    Settings(_env_file=None,**base).validate_runtime()
    for field,value in [('public_origin','http://admin.example.com'),('encryption_key',''),('seed_demo',True),('database_url','sqlite:///demo.db'),('qdrant_url','')]:
        with pytest.raises(ValueError):Settings(_env_file=None,**{**base,field:value}).validate_runtime()


@pytest.mark.parametrize('provider',['openai','anthropic','gemini'])
def test_official_adapter_wire_roundtrip(provider,monkeypatch):
    captured=[]
    def handler(request):
        data=json.loads(request.content);captured.append((request,data));n=len(captured)
        if provider=='openai':
            output=[{'type':'function_call','id':'fc1','call_id':'call1','name':'connection_check','arguments':'{"nonce":"daywork"}','status':'completed'}] if n==1 else [{'type':'message','id':'msg1','role':'assistant','status':'completed','content':[{'type':'output_text','text':'OK','annotations':[]}]}]
            return httpx.Response(200,json={'id':'resp1','object':'response','created_at':1,'model':'test-model','status':'completed','output':output})
        if provider=='gemini':
            message={'role':'assistant','content':None,'tool_calls':[{'id':'call1','type':'function','function':{'name':'connection_check','arguments':'{"nonce":"daywork"}'},'extra_content':{'google':{'thought_signature':'opaque'}}}]} if n==1 else {'role':'assistant','content':'OK'}
            return httpx.Response(200,json={'id':'chat1','object':'chat.completion','created':1,'model':'test-model','choices':[{'index':0,'message':message,'finish_reason':'tool_calls' if n==1 else 'stop'}]})
        blocks=[{'type':'tool_use','id':'call1','name':'connection_check','input':{'nonce':'daywork'}}] if n==1 else [{'type':'text','text':'OK'}]
        return httpx.Response(200,json={'id':'msg1','role':'assistant','type':'message','content':blocks,'stop_reason':'tool_use' if n==1 else 'end_turn'})
    transport=httpx.MockTransport(handler)
    real_http=httpx.Client
    real_openai=providers.OpenAI
    if provider=='anthropic':
        monkeypatch.setattr(providers.httpx,'Client',lambda **kw:real_http(transport=transport,**kw))
    else:
        monkeypatch.setattr(providers,'OpenAI',lambda **kw:real_openai(http_client=real_http(transport=transport),**kw))
    providers.probe(provider,'test-model',KEY)
    assert len(captured)==2
    request,data=captured[0]
    assert request.url.host=={'openai':'api.openai.com','gemini':'generativelanguage.googleapis.com','anthropic':'api.anthropic.com'}[provider]
    assert KEY not in json.dumps(data)
    second=captured[1][1]
    if provider=='anthropic':
        assert request.headers['x-api-key']==KEY
        assert second['messages'][-1]['content'][0]['tool_use_id']=='call1'
    elif provider=='gemini':
        assert second['messages'][-1]['tool_call_id']=='call1'
        assert second['messages'][-2]['tool_calls'][0]['extra_content']['google']['thought_signature']=='opaque'
    else:
        assert second['input'][-1]['call_id']=='call1' and second['store'] is False

@pytest.mark.parametrize('decision',['approved','rejected'])
def test_leave_review_end_to_end_and_replay(client,decision):
    add_employee(client)
    admin_token=client.cookies.get('daywork_session');admin_csrf=client.headers['X-CSRF-Token']
    login_staff(client)
    staff_token=client.cookies.get('daywork_session');staff_csrf=client.headers['X-CSRF-Token']
    draft=client.post('/api/leaves/draft',json=payload()).json()
    assert client.post('/api/leaves/'+draft['id']+'/confirm').status_code==200
    assert client.post('/api/admin/leaves/'+draft['id']+'/review',json={'decision':decision,'note':'review'}).status_code==403
    client.cookies.clear();client.cookies.set('daywork_session',admin_token);client.headers['X-CSRF-Token']=admin_csrf
    rows=client.get('/api/admin/leaves').json();assert rows[0]['employee_id']=='STAFF001'
    for _ in range(2):
        r=client.post('/api/admin/leaves/'+draft['id']+'/review',json={'decision':decision,'note':'同意' if decision=='approved' else '請調整日期'})
        assert r.status_code==200 and r.json()['status']==decision
    opposite='approved' if decision=='rejected' else 'rejected'
    assert client.post('/api/admin/leaves/'+draft['id']+'/review',json={'decision':opposite,'note':'change'}).status_code==409
    client.cookies.clear();client.cookies.set('daywork_session',staff_token);client.headers['X-CSRF-Token']=staff_csrf
    hours=client.get('/api/dashboard').json()['balances'][0]['hours']
    assert hours==(19 if decision=='approved' else 24)
    events=client.get('/api/leaves/'+draft['id']+'/events').json()
    assert len([e for e in events if e['action']==decision])==1 and events[-1]['note']
    if decision=='approved':
        assert client.post('/api/leaves/draft',json=payload()).status_code==422


def test_no_self_review(client):
    draft=client.post('/api/leaves/draft',json=payload()).json()
    assert client.post('/api/leaves/'+draft['id']+'/confirm').status_code==200
    assert client.post('/api/admin/leaves/'+draft['id']+'/review',json={'decision':'approved','note':'self'}).status_code==403

def test_startup_rejects_wrong_master_key(client,monkeypatch):
    import asyncio
    new_provider(client)
    monkeypatch.setattr(main.settings,'encryption_key',Fernet.generate_key().decode())
    async def attempt():
        async with main.lifespan(main.app):
            pytest.fail('Wrong master key should prevent startup')
    with pytest.raises(ValueError,match='加密主密鑰'):
        asyncio.run(attempt())
