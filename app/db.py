from datetime import date, datetime, time, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo
from sqlalchemy import create_engine, ForeignKey, UniqueConstraint, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.types import JSON
from app.config import settings, ROOT

TAIPEI = ZoneInfo('Asia/Taipei')


def today():
    return datetime.now(TAIPEI).date()


def uid():
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Employee(Base):
    __tablename__ = 'employees'
    id: Mapped[str] = mapped_column(primary_key=True)
    name: Mapped[str]
    department: Mapped[str]


class Balance(Base):
    __tablename__ = 'balances'
    __table_args__ = (UniqueConstraint('employee_id', 'leave_type'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey('employees.id'))
    leave_type: Mapped[str]
    hours: Mapped[float]


class Leave(Base):
    __tablename__ = 'leave_requests'
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    employee_id: Mapped[str] = mapped_column(ForeignKey('employees.id'))
    leave_type: Mapped[str]
    start: Mapped[datetime]
    end: Mapped[datetime]
    hours: Mapped[float]
    reason: Mapped[str]
    status: Mapped[str] = mapped_column(default='draft')


class CalendarEvent(Base):
    __tablename__ = 'calendar_events'
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    employee_id: Mapped[str] = mapped_column(ForeignKey('employees.id'))
    title: Mapped[str]
    start: Mapped[datetime]
    end: Mapped[datetime]
    location: Mapped[str]


class Conversation(Base):
    __tablename__ = 'conversations'
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    employee_id: Mapped[str] = mapped_column(ForeignKey('employees.id'))
    messages: Mapped[list] = mapped_column(JSON, default=list)


class ChatReceipt(Base):
    """Committed together with the reply so a transport retry cannot repeat a turn."""
    __tablename__ = 'chat_receipts'
    id: Mapped[str] = mapped_column(primary_key=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey('employees.id'))
    signature: Mapped[str]
    response: Mapped[dict] = mapped_column(JSON)


class LeaveEvent(Base):
    __tablename__ = 'leave_events'
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    leave_id: Mapped[str] = mapped_column(ForeignKey('leave_requests.id'), index=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey('employees.id'))
    action: Mapped[str]
    hours: Mapped[float]
    occurred_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(TAIPEI).replace(tzinfo=None))


class Account(Base):
    __tablename__ = 'accounts'
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    username: Mapped[str] = mapped_column(unique=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey('employees.id'), unique=True)
    password_hash: Mapped[str]
    role: Mapped[str] = mapped_column(default='employee')
    disabled: Mapped[bool] = mapped_column(default=False)


class LoginSession(Base):
    __tablename__ = 'login_sessions'
    token_hash: Mapped[str] = mapped_column(primary_key=True)
    account_id: Mapped[str] = mapped_column(ForeignKey('accounts.id'), index=True)
    csrf: Mapped[str]
    expires_at: Mapped[datetime]


class AIProvider(Base):
    __tablename__ = 'ai_providers'
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    name: Mapped[str]
    provider: Mapped[str]
    model: Mapped[str]
    encrypted_key: Mapped[str]
    key_suffix: Mapped[str]
    revision: Mapped[int] = mapped_column(default=1)
    tested: Mapped[bool] = mapped_column(default=False)
    enabled: Mapped[bool] = mapped_column(default=False)


class AppOption(Base):
    __tablename__ = 'app_options'
    key: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[str]


class AdminEvent(Base):
    __tablename__ = 'admin_events'
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    actor: Mapped[str]
    action: Mapped[str]
    target: Mapped[str]
    occurred_at: Mapped[datetime] = mapped_column(default=lambda: datetime.now(TAIPEI).replace(tzinfo=None))


class LeaveReview(Base):
    __tablename__ = 'leave_reviews'
    leave_id: Mapped[str] = mapped_column(ForeignKey('leave_requests.id'), primary_key=True)
    reviewer_id: Mapped[str] = mapped_column(ForeignKey('accounts.id'))
    decision: Mapped[str]
    note: Mapped[str]


def make_engine(url):
    return create_engine(url, connect_args={'check_same_thread': False, 'timeout': 20} if url.startswith('sqlite') else {})


(ROOT / 'data').mkdir(exist_ok=True)
engine = make_engine(settings.database_url)
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def seed(engine, demo=True):
    Base.metadata.create_all(engine)
    if not demo:
        return
    with sessionmaker(engine)() as db:
        if db.get(Employee, 'E001'):
            return
        monday = today() + timedelta(days=7 - today().weekday())
        for i, name in enumerate(['陳以安', '林宇辰', '王佳寧', '李昀蓁', '張家豪', '黃品萱', '吳承恩', '劉子晴', '蔡柏宇', '楊心妤'], 1):
            eid = f'E{i:03}'
            db.add(Employee(id=eid, name=name, department='產品研發部' if i <= 5 else '營運管理部'))
            db.flush()
            for kind, hours in [('annual', 36), ('compensatory', 12)]:
                db.add(Balance(employee_id=eid, leave_type=kind, hours=hours))
            for offset, title, hour, location in [(0, '產品團隊週會', 10, '會議室 A / 線上'), (2, 'AI 專案進度同步', 14, '會議室 B')]:
                start = datetime.combine(monday + timedelta(days=offset), time(hour))
                db.add(CalendarEvent(employee_id=eid, title=title, start=start, end=start + timedelta(hours=1), location=location))
        db.commit()
