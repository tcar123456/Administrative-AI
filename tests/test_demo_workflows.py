from uuid import uuid4
from app import main
from app.db import CalendarEvent
from test_app import client, next_monday, payload
from test_security import add_employee, login_staff


def event_payload(**overrides):
    day = next_monday().isoformat()
    return {'id': str(uuid4()), 'title': 'Demo 彩排', 'start': day + 'T10:00:00+08:00',
            'end': day + 'T11:00:00+08:00', 'location': '會議室 A', **overrides}


def test_calendar_crud_replay_and_agent_visibility(client):
    body = event_payload()
    first = client.post('/api/calendar', json=body)
    assert first.status_code == 200
    row = first.json()
    assert row['start'].endswith('10:00:00')
    assert client.post('/api/calendar', json=body).json() == row
    assert client.post('/api/calendar', json={**body, 'title': 'different'}).status_code == 409
    assert sum(e['id'] == row['id'] for e in client.get('/api/calendar').json()) == 1
    assert 'Demo 彩排' in client.post('/api/chat', json={'message': '我下週有哪些會議？'}).json()['answer']
    update = {k: v for k, v in body.items() if k != 'id'}
    update.update(title='Demo 正式展示', start=body['start'].replace('10:00:00+08:00', '04:00:00Z'), end=body['end'].replace('11:00:00+08:00', '05:00:00Z'))
    edited = client.put('/api/calendar/' + row['id'], json=update)
    assert edited.status_code == 200 and edited.json()['start'].endswith('12:00:00')
    answer = client.post('/api/chat', json={'message': '我下週有哪些會議？'}).json()['answer']
    assert 'Demo 正式展示' in answer and 'Demo 彩排' not in answer
    assert client.delete('/api/calendar/' + row['id']).status_code == 200
    assert all(e['id'] != row['id'] for e in client.get('/api/calendar').json())


def test_calendar_validation_and_employee_isolation(client):
    body = event_payload()
    assert client.post('/api/calendar', json={**body, 'title': '  '}).status_code == 422
    assert client.post('/api/calendar', json={**body, 'end': body['start']}).status_code == 422
    assert client.post('/api/calendar', json={**body, 'employee_id': 'E002'}).status_code == 422
    assert client.post('/api/calendar', json=body, headers={'X-CSRF-Token': ''}).status_code == 403
    row = client.post('/api/calendar', json=body).json()
    assert client.get('/api/calendar?start_date=2027-01-02&end_date=2027-01-01').status_code == 422
    assert client.get('/api/calendar?start_date=2027-01-01&end_date=2028-01-01').status_code == 422
    add_employee(client)
    login_staff(client)
    assert client.get('/api/calendar').json() == []
    assert client.put('/api/calendar/' + row['id'], json={k: v for k, v in body.items() if k != 'id'}).status_code == 404
    assert client.delete('/api/calendar/' + row['id']).status_code == 404
    assert client.post('/api/calendar', json=body).status_code == 409
    with main.SessionLocal() as db:
        assert db.get(CalendarEvent, row['id']).title == body['title']
    assert client.post('/api/calendar', json=event_payload()).status_code == 200


def test_review_workbench_counts_history_and_access(client):
    own = client.post('/api/leaves/draft', json=payload()).json()
    client.post('/api/leaves/' + own['id'] + '/confirm')
    add_employee(client)
    login_staff(client)
    staff = client.post('/api/leaves/draft', json=payload()).json()
    client.post('/api/leaves/' + staff['id'] + '/confirm')
    assert client.get('/api/admin/leaves/summary').status_code == 403
    assert client.get('/api/admin/leaves?status=approved').status_code == 403
    result = client.post('/api/auth/login', json={'username': 'admin', 'password': 'test-password-123'}).json()
    client.headers['X-CSRF-Token'] = result['csrf']
    assert client.get('/api/admin/leaves/summary').json() == {'pending': 2, 'actionable': 1, 'approved': 0, 'rejected': 0}
    pending = client.get('/api/admin/leaves').json()
    assert not next(r for r in pending if r['id'] == own['id'])['can_review']
    client.post('/api/admin/leaves/' + staff['id'] + '/review', json={'decision': 'approved', 'note': '交接已完成'})
    history = client.get('/api/admin/leaves?status=approved').json()
    assert len(history) == 1 and history[0]['review_note'] == '交接已完成'
    assert history[0]['reviewer'] == 'admin' and history[0]['department'] == '人資'
    assert not history[0]['can_review']
    assert client.get('/api/admin/leaves/summary').json() == {'pending': 1, 'actionable': 0, 'approved': 1, 'rejected': 0}


def test_citation_version_matches_documents_and_changes_on_reindex(client, tmp_path):
    source = main.app.state.kb.search('報帳期限')[0]
    doc = next(d for d in client.get('/api/documents').json() if d['source'] == source['source'])
    assert doc['fingerprint'] == source['fingerprint']
    assert any(c['page'] == source['page'] and c['chunk'] == source['chunk'] for c in doc['chunks'])
    folder = tmp_path / 'updated'
    folder.mkdir()
    (folder / source['source']).write_text('# 報帳期限\n新版報帳期限說明。', encoding='utf-8')
    main.app.state.kb.ingest(folder)
    assert client.get('/api/documents').json()[0]['fingerprint'] != source['fingerprint']
