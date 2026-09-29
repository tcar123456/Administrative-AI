"""Initial credentials for an explicitly enabled demonstration database."""
from sqlalchemy import select
from app.db import Account, AdminEvent, Employee
from app.security import passwords


def initialize_demo_admin(sessions, settings):
    if not (settings.demo_available and settings.seed_demo and settings.demo_admin):
        return
    with sessions.begin() as db:
        # Never replace an existing account or restore a changed demo password.
        if db.scalar(select(Account.id).limit(1)) is not None:
            return
        if db.get(Employee, 'E001') is None:
            raise ValueError('示範管理員需要先建立 E001 示範員工。')
        user = Account(username='admin', employee_id='E001',
                       password_hash=passwords.hash('000000'), role='admin')
        db.add(user)
        db.flush()
        db.add(AdminEvent(actor='demo-bootstrap', action='demo_admin_created', target=user.id))
