"""Opt-in test for disposable PostgreSQL / Qdrant services (CI creates both)."""
import os
from datetime import timedelta
import pytest
from fastapi.testclient import TestClient


@pytest.mark.skipif(os.getenv('RUN_SERVICE_TESTS') != '1', reason='Requires disposable PostgreSQL and Qdrant services')
def test_postgres_qdrant_end_to_end():
    from app.main import app
    from app.config import settings
    from app.db import today
    assert settings.agent_mode == 'demo'
    assert settings.database_url.startswith('postgresql') and settings.qdrant_url
    with TestClient(app, base_url=settings.public_origin, headers={'Origin':settings.public_origin}) as client:
        assert client.get('/api/auth/status').json()['setup_required'] is False
        result=client.post('/api/auth/login',json={'username':'admin','password':'000000'})
        assert result.status_code==200, result.text
        client.headers['X-CSRF-Token']=result.json()['csrf']
        assert client.get('/api/health').status_code == 200
        result = client.post('/api/chat', json={'message': '國外出差一天餐費可以報多少？'}).json()
        assert result['sources'][0]['source'] == 'travel_policy.md'
        balance = client.get('/api/dashboard').json()['balances']
        day = today() + timedelta(days=7-today().weekday())
        draft = client.post('/api/leaves/draft', json={'leave_type': 'annual', 'start_time': f'{day}T09:00:00', 'end_time': f'{day}T12:00:00', 'reason': 'Disposable service integration test'}).json()
        assert client.post(f'/api/leaves/{draft["id"]}/confirm').status_code == 200
        assert client.post(f'/api/leaves/{draft["id"]}/withdraw').status_code == 200
        assert client.get('/api/dashboard').json()['balances'] == balance
