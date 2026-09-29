import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from uuid import uuid4
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker
from app import main
from app.config import Settings
from app.db import make_engine, Balance, Conversation, Leave, today
from app.agent import Runner, parse_demo_leave, definitions
from app.leave_service import confirm_leave, withdraw_leave


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, agent_mode='demo', demo_admin=False, database_url=f'sqlite:///{tmp_path / "test.db"}', qdrant_path=str(tmp_path/'qdrant'), qdrant_url='', public_origin='http://testserver', secret_dir=str(tmp_path/'secrets'))
    engine = make_engine(settings.database_url)
    monkeypatch.setattr(main, 'engine', engine)
    monkeypatch.setattr(main, 'SessionLocal', sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(main, 'settings', settings)
    with TestClient(main.app, headers={'Origin':'http://testserver'}) as client:
        result=client.post('/api/auth/setup',json={'username':'admin','password':'test-password-123','name':'陳以安','token':(tmp_path/'secrets/setup-token').read_text()})
        assert result.status_code==200, result.text
        client.headers['X-CSRF-Token']=result.json()['csrf']
        yield client
    engine.dispose()


def next_monday():
    return today() + timedelta(days=7-today().weekday())


def payload(**overrides):
    day = next_monday().isoformat()
    return {'leave_type':'annual','start_time':day+'T13:00:00+08:00','end_time':day+'T18:00:00+08:00','reason':'測試私人事務',**overrides}


def annual(client):
    return next(b['hours'] for b in client.get('/api/dashboard').json()['balances'] if b['leave_type']=='annual')


def test_dashboard_and_documents(client):
    dashboard = client.get('/api/dashboard').json()
    assert dashboard['employee']['id']=='E001'
    assert len(dashboard['events'])==2
    assert annual(client)==36
    assert len(client.get('/api/documents').json())==6
    assert client.get('/').status_code==200


def test_rag_and_compound_tools_memory(client):
    result=client.post('/api/chat',json={'message':'特休規定是什麼？我還剩多少假？'}).json()
    assert [x['tool'] for x in result['trace']]==['search_company_knowledge','get_leave_balance']
    assert 'leave_policy.md' in [s['source'] for s in result['sources']]
    assert '36' in result['answer']
    history=client.get('/api/conversations/'+result['conversation_id']).json()
    assert len(history['messages'])==2
    second=client.post('/api/chat',json={'message':'我下週有哪些會議？','conversation_id':result['conversation_id']}).json()
    assert len(second['trace'][0]['result']['events'])==2
    assert len(client.get('/api/conversations/'+result['conversation_id']).json()['messages'])==4


def test_travel_retrieval_and_unknown(client):
    result=client.post('/api/chat',json={'message':'國外出差一天餐費可以報多少？'}).json()
    assert result['sources'][0]['source']=='travel_policy.md'
    assert '1,500' in result['answer']
    assert main.app.state.kb.search('zzzzzzzzzz')==[]


def test_draft_confirm_idempotent_and_overlap(client):
    draft=client.post('/api/leaves/draft',json=payload()).json()
    assert draft['status']=='draft' and draft['hours']==5
    assert annual(client)==36
    assert client.post('/api/leaves/draft',json=payload()).json()['id']==draft['id']
    for _ in range(2):
        assert client.post('/api/leaves/'+draft['id']+'/confirm').json()['status']=='pending'
    assert annual(client)==31
    assert client.post('/api/leaves/draft',json=payload()).status_code==422
    assert client.post('/api/leaves/'+draft['id']+'/cancel').status_code==409


def test_cancelled_draft(client):
    draft=client.post('/api/leaves/draft',json=payload()).json()
    assert client.post('/api/leaves/'+draft['id']+'/cancel').status_code==200
    assert client.post('/api/leaves/'+draft['id']+'/confirm').status_code==409
    assert annual(client)==36


def test_recheck_balance_before_confirm(client):
    draft=client.post('/api/leaves/draft',json=payload()).json()
    with main.SessionLocal.begin() as db:
        db.execute(update(Balance).where(Balance.employee_id=='E001',Balance.leave_type=='annual').values(hours=1))
    assert client.post('/api/leaves/'+draft['id']+'/confirm').status_code==409
    assert annual(client)==1
    assert client.post('/api/leaves/draft',json=payload()).status_code==422


@pytest.mark.parametrize('start,end',[
    ('08:00','10:00'),('12:00','14:00'),('11:00','12:30'),('13:00','13:00'),('14:00','13:00'),('13:15','14:00'),('13:00','19:00'),
])
def test_invalid_hours(client,start,end):
    day=next_monday().isoformat()
    response=client.post('/api/leaves/draft',json=payload(start_time=f'{day}T{start}:00',end_time=f'{day}T{end}:00'))
    assert response.status_code==422


def test_weekend_past_cross_day_and_lunch(client):
    for start,end in [(next_monday()+timedelta(days=5),next_monday()+timedelta(days=5)),(today()-timedelta(days=2),today()-timedelta(days=2)),(next_monday(),next_monday()+timedelta(days=1))]:
        assert client.post('/api/leaves/draft',json=payload(start_time=f'{start}T09:00:00',end_time=f'{end}T18:00:00')).status_code==422
    day=next_monday().isoformat()
    assert client.post('/api/leaves/draft',json=payload(start_time=day+'T09:00:00')).json()['hours']==8


def test_timezone_conversion(client):
    day=next_monday().isoformat()
    result=client.post('/api/leaves/draft',json=payload(start_time=day+'T05:00:00Z',end_time=day+'T10:00:00Z')).json()
    assert result['start'].endswith('13:00:00') and result['hours']==5


def test_identity_and_foreign_records(client):
    assert client.post('/api/chat',json={'message':'餘額','employee_id':'E002'}).status_code==422
    assert client.post('/api/leaves/draft',json=payload(employee_id='E002')).status_code==422
    with main.SessionLocal.begin() as db:
        conv=Conversation(employee_id='E002',messages=[])
        leave=Leave(employee_id='E002',leave_type='annual',start=datetime.combine(next_monday(),time(13)),end=datetime.combine(next_monday(),time(18)),hours=5,reason='其他員工')
        db.add_all([conv,leave]);db.flush();cid,lid=conv.id,leave.id
    assert client.get('/api/conversations/'+cid).status_code==404
    assert client.post('/api/chat',json={'message':'餘額','conversation_id':cid}).status_code==404
    assert client.post('/api/leaves/'+lid+'/confirm').status_code==404
    assert client.post('/api/leaves/'+lid+'/cancel').status_code==409
    with main.SessionLocal() as db:
        runner=Runner(db,'E001',main.app.state.kb,main.settings)
        assert 'error' in runner.call('get_leave_balance',{'employee_id':'E002'})
        assert 'error' in runner.call('delete_database',{})


def test_concurrent_confirmation(client):
    first=client.post('/api/leaves/draft',json=payload()).json()
    second=client.post('/api/leaves/draft',json=payload(leave_type='compensatory')).json()
    def confirm(lid):
        try:
            with main.SessionLocal.begin() as db:
                return confirm_leave(db,'E001',lid)['status']
        except ValueError:
            return 'overlap'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(confirm,[first['id'],second['id']]))
    assert sorted(results)==['overlap','pending']
    balances=client.get('/api/dashboard').json()['balances']
    assert sum(b['hours'] for b in balances)==43


def test_demo_leave_and_missing_fields(client):
    result=client.post('/api/chat',json={'message':'幫我請下週五下午特休，原因：私人事務'}).json()
    assert result['drafts'][0]['hours']==5
    assert annual(client)==36
    missing=client.post('/api/chat',json={'message':'幫我請下週五下午特休'}).json()
    assert not missing['drafts'] and '原因' in missing['answer']
    assert datetime.fromisoformat(parse_demo_leave('幫我請下週天下午特休，原因：測試')['start_time']).weekday()==6


def test_live_tool_loop_mock(client,monkeypatch):
    import openai
    captured=[]
    responses=[SimpleNamespace(output=[SimpleNamespace(type='function_call',name='get_leave_balance',arguments='{}',call_id='balance-1')],output_text=''),SimpleNamespace(output=[],output_text='你還有 36 小時特休。')]
    def create(**kwargs):
        captured.append(kwargs)
        return responses.pop(0)
    monkeypatch.setattr(openai,'OpenAI',lambda **kwargs:SimpleNamespace(responses=SimpleNamespace(create=create)))
    with main.SessionLocal() as db:
        settings=main.settings.model_copy(update={'agent_mode':'openai','openai_api_key':'test-key'})
        result=Runner(db,'E001',main.app.state.kb,settings).run('我剩多少假？',[])
    assert result['trace'][0]['tool']=='get_leave_balance'
    assert captured[-1]['input'][-1]['type']=='function_call_output'
    assert json.loads(captured[-1]['input'][-1]['output'])['balances'][0]['hours']==36
    assert all(d['parameters']['additionalProperties'] is False for d in definitions())


def test_agent_failure_rolls_back_drafts(client,monkeypatch):
    def broken(self,message,history):
        self.call('create_leave_request',payload())
        raise RuntimeError('provider failed')
    monkeypatch.setattr(Runner,'run',broken)
    assert client.post('/api/chat',json={'message':'請假'}).status_code==503
    assert client.get('/api/dashboard').json()['leaves']==[]


def test_rag_ingestion_idempotent_and_replace(client,tmp_path):
    folder=tmp_path/'kb';folder.mkdir()
    (folder/'test.md').write_text('# 測試政策\n獨角獸設備每年提供一台。',encoding='utf-8')
    kb=main.app.state.kb
    kb.ingest(folder);kb.ingest(folder)
    assert len(kb.documents())==1
    assert kb.search('獨角獸')[0]['source']=='test.md'
    (folder/'test.md').write_text('# 測試政策\n咖啡豆補助每月一百元。',encoding='utf-8')
    kb.ingest(folder)
    assert '獨角獸' not in str(kb.documents())


def test_request_replay_is_idempotent(client):
    body={'message':'幫我請下週五下午特休，原因：重試測試','request_id':str(uuid4())}
    first=client.post('/api/chat',json=body).json()
    second=client.post('/api/chat',json=body).json()
    assert first==second
    assert len(client.get('/api/conversations').json())==1
    assert len(client.get('/api/conversations/'+first['conversation_id']).json()['messages'])==2
    assert len(client.get('/api/dashboard').json()['leaves'])==1
    assert client.post('/api/chat',json={**body,'message':'不同的訊息'}).status_code==409


def test_history_lists_only_current_employee(client):
    result=client.post('/api/chat',json={'message':'我剩多少假？'}).json()
    with main.SessionLocal.begin() as db:
        db.add(Conversation(employee_id='E002',messages=[{'role':'user','content':'私人內容'}]))
    listed=client.get('/api/conversations').json()
    assert len(listed)==1 and listed[0]['id']==result['conversation_id']
    assert listed[0]['updated_at'] and listed[0]['message_count']==2


def test_followup_fills_leave_reason(client):
    first=client.post('/api/chat',json={'message':'幫我請下週五下午特休'}).json()
    assert first['pending_leave']['leave_type']=='annual' and not first['drafts']
    second=client.post('/api/chat',json={'message':'原因：私人事務','conversation_id':first['conversation_id']}).json()
    assert second['drafts'][0]['reason']=='私人事務'
    assert second['pending_leave'] is None
    assert annual(client)==36


def test_reason_keywords_do_not_change_intent(client):
    first=client.post('/api/chat',json={'message':'幫我請下週五下午特休'}).json()
    second=client.post('/api/chat',json={'message':'原因：處理出差後的私人行程','conversation_id':first['conversation_id']}).json()
    assert second['drafts'][0]['reason']=='處理出差後的私人行程'
    assert [t['tool'] for t in second['trace']]==['get_leave_balance','create_leave_request']


def test_empty_slots_can_be_filled_across_turns(client):
    first=client.post('/api/chat',json={'message':'我要請假'}).json()
    assert first['pending_leave']=={}
    second=client.post('/api/chat',json={'message':'下週五下午特休，原因：私人事務','conversation_id':first['conversation_id']}).json()
    assert len(second['drafts'])==1


def test_insufficient_balance_switch_is_explicit(client):
    with main.SessionLocal.begin() as db:
        db.execute(update(Balance).where(Balance.employee_id=='E001',Balance.leave_type=='annual').values(hours=0))
    first=client.post('/api/chat',json={'message':'幫我請下週五下午特休，原因：休息'}).json()
    assert not first['drafts'] and '餘額不足' in first['answer']
    ack=client.post('/api/chat',json={'message':'好','conversation_id':first['conversation_id']}).json()
    assert not ack['drafts'] and '不會自動送出' in ack['answer']
    second=client.post('/api/chat',json={'message':'改用補休','conversation_id':first['conversation_id']}).json()
    assert second['drafts'][0]['leave_type']=='compensatory'
    assert second['drafts'][0]['status']=='draft'


def test_followup_does_not_leak_between_conversations(client):
    client.post('/api/chat',json={'message':'幫我請下週五下午特休'})
    other=client.post('/api/chat',json={'message':'原因：私人事務'}).json()
    assert not other['drafts'] and other['pending_leave'] is None


def test_followup_can_be_abandoned(client):
    first=client.post('/api/chat',json={'message':'我要請假'}).json()
    result=client.post('/api/chat',json={'message':'算了','conversation_id':first['conversation_id']}).json()
    assert result['pending_leave'] is None and not result['drafts']


def test_precise_time_and_invalid_date(client):
    result=client.post('/api/chat',json={'message':'幫我請下週五 09:00–11:30 特休，原因：看診'}).json()
    assert result['drafts'][0]['hours']==2.5
    bad=client.post('/api/chat',json={'message':'幫我請 2027/02/30 上午特休，原因：測試'}).json()
    assert '日期不存在' in bad['answer'] and not bad['drafts']


def test_calendar_today_is_not_seven_days(client):
    result=client.post('/api/chat',json={'message':'今天有哪些會議？'}).json()
    args=result['trace'][0]['arguments']
    assert args['start_date']==args['end_date']==today().isoformat()


def test_withdraw_refunds_once_and_records_events(client):
    draft=client.post('/api/leaves/draft',json=payload()).json()
    assert client.post('/api/leaves/'+draft['id']+'/confirm').status_code==200
    for _ in range(2):
        assert client.post('/api/leaves/'+draft['id']+'/withdraw').json()['status']=='withdrawn'
    assert annual(client)==36
    assert client.post('/api/leaves/'+draft['id']+'/confirm').status_code==409
    events=client.get('/api/leaves/'+draft['id']+'/events').json()
    assert [e['action'] for e in events]==['draft_created','submitted','withdrawn']
    assert client.post('/api/leaves/draft',json=payload()).status_code==200


def test_withdraw_concurrency(client):
    row=client.post('/api/leaves/draft',json=payload()).json()
    client.post('/api/leaves/'+row['id']+'/confirm')
    def withdraw(_):
        with main.SessionLocal.begin() as db:
            return withdraw_leave(db,'E001',row['id'])['status']
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(withdraw,range(2)))==['withdrawn','withdrawn']
    assert annual(client)==36
    assert len(client.get('/api/leaves/'+row['id']+'/events').json())==3


def test_cannot_withdraw_foreign_or_started_leave(client):
    row=client.post('/api/leaves/draft',json=payload()).json()
    client.post('/api/leaves/'+row['id']+'/confirm')
    with main.SessionLocal.begin() as db:
        db.execute(update(Leave).where(Leave.id==row['id']).values(start=datetime.now()-timedelta(days=2)))
    assert client.post('/api/leaves/'+row['id']+'/withdraw').status_code==409
    assert annual(client)==31
    with main.SessionLocal.begin() as db:
        db.execute(update(Leave).where(Leave.id==row['id']).values(employee_id='E002'))
    assert client.post('/api/leaves/'+row['id']+'/withdraw').status_code==404
    assert client.get('/api/leaves/'+row['id']+'/events').status_code==404


def test_cancel_replay_does_not_duplicate_audit(client):
    row=client.post('/api/leaves/draft',json=payload()).json()
    for _ in range(2):
        assert client.post('/api/leaves/'+row['id']+'/cancel').status_code==200
    assert len(client.get('/api/leaves/'+row['id']+'/events').json())==2


def test_health_detects_missing_index(client,monkeypatch):
    assert client.get('/api/health').json()['checks']=={'database':True,'knowledge':True}
    monkeypatch.setattr(main.app.state.kb.client,'collection_exists',lambda _:False)
    assert client.get('/api/health').status_code==503


def test_live_followup_includes_current_leave_status(client,monkeypatch):
    import openai
    first=client.post('/api/chat',json={'message':'幫我請下週五下午特休，原因：測試'}).json()
    client.post('/api/leaves/'+first['drafts'][0]['id']+'/confirm')
    history=client.get('/api/conversations/'+first['conversation_id']).json()['messages']
    captured=[]
    def create(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(output=[],output_text='申請目前待審核。')
    monkeypatch.setattr(openai,'OpenAI',lambda **kwargs:SimpleNamespace(responses=SimpleNamespace(create=create)))
    with main.SessionLocal() as db:
        Runner(db,'E001',main.app.state.kb,main.settings.model_copy(update={'agent_mode':'openai','openai_api_key':'test'})).run('剛才的申請狀態？',history)
    assert '"status": "pending"' in captured[0]['input'][1]['content']


def test_live_loop_limit(client,monkeypatch):
    import openai
    def create(**kwargs):
        return SimpleNamespace(output=[SimpleNamespace(type='function_call',name='get_leave_balance',arguments='{}',call_id=str(uuid4()))],output_text='')
    monkeypatch.setattr(openai,'OpenAI',lambda **kwargs:SimpleNamespace(responses=SimpleNamespace(create=create)))
    with main.SessionLocal() as db:
        result=Runner(db,'E001',main.app.state.kb,main.settings.model_copy(update={'agent_mode':'openai','openai_api_key':'test'})).run('查餘額',[])
    assert len(result['trace'])==6 and '步數上限' in result['answer']


@pytest.mark.parametrize('query,expected',[
    ('國外出差一天餐費可以報多少？','travel_policy.md'),
    ('報帳期限是多久？','expense_policy.md'),
    ('補休多久到期？','overtime_policy.md'),
    ('密碼可以共用嗎？','security_policy.md'),
    ('學習補助有多少？','equipment_policy.md'),
    ('特休最小申請單位是什麼？','leave_policy.md'),
])
def test_retrieval_evaluation(client,query,expected):
    hits=main.app.state.kb.search(query)
    assert hits and hits[0]['source']==expected
    if expected=='travel_policy.md':
        assert {hit['source'] for hit in hits}=={'travel_policy.md'}


@pytest.mark.parametrize('query',['今天股價多少？','恐龍為何滅絕？','公司股票分紅政策是什麼？'])
def test_retrieval_abstains_without_evidence(client,query):
    assert main.app.state.kb.search(query)==[]


def test_document_chunks_are_ordered(client,tmp_path):
    folder=tmp_path/'long';folder.mkdir()
    (folder/'long.md').write_text('# 設備規範\n'+('螢幕設備申請。'*160),encoding='utf-8')
    main.app.state.kb.ingest(folder)
    chunks=main.app.state.kb.documents()[0]['chunks']
    assert len(chunks)>1 and [c['chunk'] for c in chunks]==list(range(1,len(chunks)+1))
