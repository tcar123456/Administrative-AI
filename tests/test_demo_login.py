import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from app import main
from app.config import Settings
from app.db import Account, AdminEvent, make_engine, seed
from app.demo_accounts import initialize_demo_admin
from app.security import passwords, verify_password


def test_demo_login_and_password_survives_restart(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, database_url=f'sqlite:///{tmp_path / "demo.db"}',
                        qdrant_path=str(tmp_path/'qdrant'), qdrant_url='',
                        public_origin='http://testserver', secret_dir=str(tmp_path/'secrets'))
    engine = make_engine(settings.database_url)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(main, 'settings', settings)
    monkeypatch.setattr(main, 'engine', engine)
    monkeypatch.setattr(main, 'SessionLocal', sessions)
    try:
        with TestClient(main.app, headers={'Origin':'http://testserver'}) as client:
            assert client.get('/api/auth/status').json()['setup_required'] is False
            assert client.get('/api/dashboard').status_code == 401
            result = client.post('/api/auth/login', json={'username':'admin','password':'000000'})
            assert result.status_code == 200 and result.json()['role'] == 'admin'
            client.headers['X-CSRF-Token'] = result.json()['csrf']
            assert client.get('/api/dashboard').json()['employee']['id'] == 'E001'
            assert client.post('/api/auth/password', json={
                'current_password':'000000','new_password':'changed-password-123'}).status_code == 200
        with TestClient(main.app, headers={'Origin':'http://testserver'}) as client:
            assert client.post('/api/auth/login', json={'username':'admin','password':'000000'}).status_code == 401
            assert client.post('/api/auth/login', json={'username':'admin','password':'changed-password-123'}).status_code == 200
        with sessions() as db:
            assert len(list(db.scalars(select(Account)))) == 1
            assert len(list(db.scalars(select(AdminEvent).where(AdminEvent.action=='demo_admin_created')))) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize('overrides,created', [
    ({'environment':'production','allow_demo':False}, False),
    ({'environment':'production','allow_demo':True}, True),
    ({'seed_demo':False}, False),
    ({'demo_admin':False}, False),
])
def test_default_credentials_only_for_demo(tmp_path, overrides, created):
    settings = Settings(_env_file=None, **overrides)
    engine = make_engine(f'sqlite:///{tmp_path / "accounts.db"}')
    sessions = sessionmaker(engine, expire_on_commit=False)
    try:
        seed(engine, settings.seed_demo)
        initialize_demo_admin(sessions, settings)
        with sessions() as db:
            user = db.scalar(select(Account))
            assert bool(user) is created
            if user:
                assert user.password_hash != '000000'
                assert verify_password(user.password_hash, '000000')
    finally:
        engine.dispose()


def test_existing_accounts_are_not_changed(tmp_path):
    engine = make_engine(f'sqlite:///{tmp_path / "existing.db"}')
    sessions = sessionmaker(engine, expire_on_commit=False)
    try:
        seed(engine, True)
        with sessions.begin() as db:
            db.add(Account(username='owner', employee_id='E001', role='admin',
                           password_hash=passwords.hash('existing-password-123'), disabled=True))
        initialize_demo_admin(sessions, Settings(_env_file=None))
        with sessions() as db:
            users = list(db.scalars(select(Account)))
            assert len(users) == 1 and users[0].username == 'owner' and users[0].disabled
            assert verify_password(users[0].password_hash, 'existing-password-123')
    finally:
        engine.dispose()
