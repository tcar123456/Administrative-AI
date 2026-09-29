"""Account administration and encrypted AI settings; all writes are CSRF protected."""
import secrets
from threading import Lock
from uuid import UUID
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, delete, update, func
from sqlalchemy.exc import IntegrityError
from app.db import Account, Employee, Balance, LoginSession, AIProvider, AppOption, AdminEvent
from app.security import admin_user, current_user, passwords, verify_password, DUMMY_HASH, issue_session, digest, setup_secret
from app.providers import probe

router = APIRouter(prefix='/api')
setup_lock = Lock()


def audit(db, user, action, target):
    db.add(AdminEvent(actor=user.username, action=action, target=str(target)))


class Body(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)


class LoginBody(Body):
    username: str = Field(pattern=r'^[a-zA-Z0-9_.@-]{3,80}$')
    password: str = Field(min_length=1, max_length=256)


class NewAccount(LoginBody):
    password: str = Field(min_length=12, max_length=128)
    name: str = Field(min_length=1, max_length=80)
    department: str = Field(min_length=1, max_length=80)
    employee_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,32}$')
    role: Literal['admin','employee'] = 'employee'
    annual_hours: float = Field(default=0, ge=0, le=2000, allow_inf_nan=False)
    compensatory_hours: float = Field(default=0, ge=0, le=2000, allow_inf_nan=False)


class SetupBody(LoginBody):
    password: str = Field(min_length=12, max_length=128)
    token: str = Field(min_length=32, max_length=256)
    name: str = Field(min_length=1, max_length=80)


def account_data(db, user):
    employee = db.get(Employee, user.employee_id)
    return {'id':user.id,'username':user.username,'role':user.role,'employee_id':user.employee_id,
            'name':employee.name,'department':employee.department,'disabled':user.disabled}


def create_account(db, body):
    if db.scalar(select(Account).where(Account.username==body.username.lower())) or db.scalar(select(Account).where(Account.employee_id==body.employee_id)):
        raise HTTPException(409, '帳號或員工編號已綁定。')
    employee = db.get(Employee, body.employee_id)
    if employee:
        raise HTTPException(409, '員工編號已存在；請使用新的編號。')
    db.add(Employee(id=body.employee_id, name=body.name, department=body.department))
    db.flush()
    for kind, hours in [('annual',body.annual_hours),('compensatory',body.compensatory_hours)]:
        db.add(Balance(employee_id=body.employee_id, leave_type=kind, hours=hours))
    user = Account(username=body.username.lower(), employee_id=body.employee_id, password_hash=passwords.hash(body.password), role=body.role)
    db.add(user)
    db.flush()
    return user


@router.get('/auth/status')
def status(request: Request):
    with request.app.state.sessions() as db:
        needed = db.scalar(select(Account.id).limit(1)) is None
    return {'setup_required':needed, 'setup_available':needed and bool(setup_secret(request.app.state.settings)),
            'environment':request.app.state.settings.environment,
            'demo_available':request.app.state.settings.demo_available}


@router.post('/auth/setup')
def setup(body: SetupBody, request: Request, response: Response):
    config = request.app.state.settings
    request.app.state.limiter.hit('setup', 5, 900)
    with setup_lock, request.app.state.sessions.begin() as db:
        if db.scalar(select(Account.id).limit(1)):
            raise HTTPException(409, '初始化已完成，請登入。')
        expected = setup_secret(config)
        if not expected or not secrets.compare_digest(digest(body.token), digest(expected)):
            raise HTTPException(403, '初始化代碼不正確。')
        # Existing local demo retains its records; production creates a clean administrator.
        if config.seed_demo and db.get(Employee, 'E001'):
            employee = db.get(Employee, 'E001')
            employee.name = body.name
            user = Account(username=body.username.lower(), employee_id='E001', password_hash=passwords.hash(body.password), role='admin')
            db.add(user)
            db.flush()
        else:
            user = create_account(db, NewAccount(username=body.username,password=body.password,name=body.name,department='管理',employee_id='ADMIN',role='admin'))
        audit(db, user, 'setup', user.id)
        csrf = issue_session(db, user, response, config)
        return {**account_data(db,user),'csrf':csrf}


@router.post('/auth/login')
def login(body: LoginBody, request: Request, response: Response):
    limiter = request.app.state.limiter
    limiter.hit('login-ip:'+str(request.client.host), 20, 900)
    limiter.hit('login-user:'+body.username.lower(), 10, 900)
    with request.app.state.sessions.begin() as db:
        user = db.scalar(select(Account).where(Account.username == body.username.lower()))
        valid = verify_password(user.password_hash if user else DUMMY_HASH, body.password)
        if not valid or not user or user.disabled:
            raise HTTPException(401, '帳號或密碼不正確，或帳號已停用。')
        if passwords.check_needs_rehash(user.password_hash):
            user.password_hash = passwords.hash(body.password)
        csrf = issue_session(db, user, response, request.app.state.settings)
        return {**account_data(db,user), 'csrf':csrf}


@router.get('/auth/me')
def me(request: Request, user=Depends(current_user)):
    with request.app.state.sessions() as db:
        return {**account_data(db,user),'csrf':request.state.csrf}


@router.post('/auth/logout')
def logout(request: Request, response: Response, user=Depends(current_user)):
    with request.app.state.sessions.begin() as db:
        db.execute(delete(LoginSession).where(LoginSession.token_hash==digest(request.cookies.get('daywork_session',''))))
    response.delete_cookie('daywork_session', path='/')
    return {'ok':True}


class PasswordBody(Body):
    current_password: str = Field(min_length=1,max_length=256)
    new_password: str = Field(min_length=12,max_length=128)


@router.post('/auth/password')
def password(body: PasswordBody, request: Request, user=Depends(current_user)):
    request.app.state.limiter.hit('password:'+user.id, 5, 900)
    with request.app.state.sessions.begin() as db:
        row=db.get(Account,user.id)
        if not verify_password(row.password_hash,body.current_password):
            raise HTTPException(403,'目前密碼不正確。')
        row.password_hash=passwords.hash(body.new_password)
        db.execute(delete(LoginSession).where(LoginSession.account_id==user.id))
        audit(db,user,'password_changed',user.id)
    return {'ok':True}


@router.get('/admin/accounts')
def accounts(request: Request, user=Depends(admin_user)):
    with request.app.state.sessions() as db:
        return [account_data(db,u) for u in db.scalars(select(Account).order_by(Account.username))]


@router.post('/admin/accounts')
def add_account(body: NewAccount, request: Request, user=Depends(admin_user)):
    try:
        with request.app.state.sessions.begin() as db:
            row=create_account(db,body)
            audit(db,user,'account_created',row.id)
            return account_data(db,row)
    except IntegrityError:
        raise HTTPException(409,'帳號或員工編號已存在。')


class DisabledBody(Body):
    disabled: bool


@router.patch('/admin/accounts/{aid}')
def toggle_account(aid: UUID, body: DisabledBody, request: Request, user=Depends(admin_user)):
    if str(aid)==user.id:
        raise HTTPException(409,'不可停用自己的帳號。')
    with request.app.state.sessions.begin() as db:
        row=db.get(Account,str(aid))
        if not row:
            raise HTTPException(404,'找不到帳號。')
        row.disabled=body.disabled
        db.execute(delete(LoginSession).where(LoginSession.account_id==row.id))
        audit(db,user,'account_disabled' if body.disabled else 'account_enabled',row.id)
        return account_data(db,row)


class ProviderBody(Body):
    name: str = Field(min_length=1,max_length=80)
    provider: Literal['openai','anthropic','gemini']
    model: str = Field(pattern=r'^[a-zA-Z0-9_.:/-]{1,120}$')
    api_key: str = Field(pattern=r'^[^\s]{16,1024}$')


def provider_data(row, default=None, admin=False):
    value = {'id':row.id,'name':row.name,'provider':row.provider,'model':row.model,'default':row.id==default}
    if admin:
        value.update(key_mask='••••••••'+row.key_suffix,tested=row.tested,enabled=row.enabled)
    return value


def default_id(db):
    row=db.get(AppOption,'default_provider')
    return row.value if row else None


@router.get('/models')
def models(request: Request, user=Depends(current_user)):
    with request.app.state.sessions() as db:
        return [provider_data(row,default_id(db)) for row in db.scalars(select(AIProvider).where(AIProvider.enabled==True, AIProvider.tested==True).order_by(AIProvider.name))]


@router.get('/admin/providers')
def providers(request: Request, user=Depends(admin_user)):
    with request.app.state.sessions() as db:
        return [provider_data(row,default_id(db),True) for row in db.scalars(select(AIProvider).order_by(AIProvider.name))]


def store_provider(db, body, request, row=None):
    if row is None:
        row=AIProvider()
        db.add(row)
        row.revision=0
    row.name, row.provider, row.model = body.name, body.provider, body.model
    row.encrypted_key=request.app.state.vault.encrypt(body.api_key.encode()).decode()
    row.key_suffix=body.api_key[-4:]
    row.enabled=False
    row.tested=False
    row.revision+=1
    db.flush()
    return row


@router.post('/admin/providers')
def add_provider(body: ProviderBody, request: Request, user=Depends(admin_user)):
    with request.app.state.sessions.begin() as db:
        row=store_provider(db,body,request)
        audit(db,user,'provider_saved',row.id)
        return provider_data(row,admin=True)


@router.put('/admin/providers/{pid}')
def replace_provider(pid: UUID, body: ProviderBody, request: Request, user=Depends(admin_user)):
    with request.app.state.sessions.begin() as db:
        row=db.get(AIProvider,str(pid),with_for_update=True)
        if not row:
            raise HTTPException(404,'找不到模型設定。')
        row=store_provider(db,body,request,row)
        db.execute(delete(AppOption).where(AppOption.key=='default_provider',AppOption.value==row.id))
        audit(db,user,'provider_rotated',row.id)
        return provider_data(row,admin=True)


@router.post('/admin/providers/{pid}/test')
def test_provider(pid: UUID, request: Request, user=Depends(admin_user)):
    request.app.state.limiter.hit('provider-test:'+user.id, 6, 60)
    with request.app.state.sessions() as db:
        row=db.get(AIProvider,str(pid))
        if not row:
            raise HTTPException(404,'找不到模型設定。')
        revision=row.revision
        try:
            key=request.app.state.vault.decrypt(row.encrypted_key.encode()).decode()
            probe(row.provider,row.model,key)
        except Exception:
            # Never include upstream response bodies, headers or key material in errors/logs.
            raise HTTPException(502,'連線或工具呼叫測試未通過。請確認 Key、模型 ID、模型工具支援、額度及網路後重試。')
    with request.app.state.sessions.begin() as db:
        changed=db.execute(update(AIProvider).where(AIProvider.id==str(pid),AIProvider.revision==revision).values(tested=True))
        if not changed.rowcount:
            raise HTTPException(409,'設定已變更，請重新測試。')
        audit(db,user,'provider_test_passed',pid)
    return {'ok':True,'message':'連線與工具呼叫往返測試成功，可啟用模型。'}


class ActivationBody(Body):
    enabled: bool
    make_default: bool = False


@router.post('/admin/providers/{pid}/activate')
def activate(pid: UUID, body: ActivationBody, request: Request, user=Depends(admin_user)):
    with request.app.state.sessions.begin() as db:
        row=db.get(AIProvider,str(pid),with_for_update=True)
        if not row:
            raise HTTPException(404,'找不到模型設定。')
        if body.enabled and not row.tested:
            raise HTTPException(409,'請先通過連線及工具呼叫測試。')
        row.enabled=body.enabled
        default=db.get(AppOption,'default_provider')
        if body.enabled and (body.make_default or not default):
            if default: default.value=row.id
            else: db.add(AppOption(key='default_provider',value=row.id))
        elif not body.enabled and default and default.value==row.id:
            db.delete(default)
        audit(db,user,'provider_enabled' if body.enabled else 'provider_disabled',pid)
        db.flush()
        return provider_data(row,default_id(db),True)


@router.delete('/admin/providers/{pid}')
def remove_provider(pid: UUID, request: Request, user=Depends(admin_user)):
    with request.app.state.sessions.begin() as db:
        row=db.get(AIProvider,str(pid),with_for_update=True)
        if not row:
            raise HTTPException(404,'找不到模型設定。')
        if row.enabled:
            raise HTTPException(409,'請先停用再刪除。')
        db.execute(delete(AppOption).where(AppOption.key=='default_provider',AppOption.value==row.id))
        db.delete(row)
        audit(db,user,'provider_deleted',pid)
    return {'ok':True}


@router.get('/admin/events')
def events(request: Request, user=Depends(admin_user)):
    with request.app.state.sessions() as db:
        return [{'actor':e.actor,'action':e.action,'target':e.target,'at':e.occurred_at.isoformat()} for e in db.scalars(select(AdminEvent).order_by(AdminEvent.occurred_at.desc()).limit(100))]

class ReviewBody(Body):
    decision: Literal['approved','rejected']
    note: str = Field(min_length=1,max_length=200)


@router.get('/admin/leaves')
def pending_leaves(request: Request, status: Literal['pending', 'approved', 'rejected'] = 'pending', user=Depends(admin_user)):
    from app.db import Leave, LeaveReview
    from app.leave_service import serialize
    with request.app.state.sessions() as db:
        rows = db.execute(select(Leave, Employee, LeaveReview, Account.username)
                          .join(Employee, Leave.employee_id == Employee.id)
                          .outerjoin(LeaveReview, LeaveReview.leave_id == Leave.id)
                          .outerjoin(Account, LeaveReview.reviewer_id == Account.id)
                          .where(Leave.status == status)
                          .order_by(Leave.start if status == 'pending' else Leave.start.desc()).limit(100))
        return [{**serialize(row), 'employee_name': employee.name, 'department': employee.department,
                 'employee_id': row.employee_id, 'can_review': status == 'pending' and row.employee_id != user.employee_id,
                 'review_note': review.note if review else '', 'reviewer': reviewer or ''}
                for row, employee, review, reviewer in rows]


@router.get('/admin/leaves/summary')
def review_summary(request: Request, user=Depends(admin_user)):
    from app.db import Leave
    with request.app.state.sessions() as db:
        counts = dict(db.execute(select(Leave.status, func.count()).group_by(Leave.status)).all())
        actionable = db.scalar(select(func.count()).select_from(Leave).where(Leave.status == 'pending', Leave.employee_id != user.employee_id))
        return {**{key: counts.get(key, 0) for key in ('pending', 'approved', 'rejected')}, 'actionable': actionable}


@router.post('/admin/leaves/{lid}/review')
def review_leave(lid: UUID, body: ReviewBody, request: Request, user=Depends(admin_user)):
    from app.db import Leave, LeaveEvent, LeaveReview
    from app.leave_service import serialize
    with request.app.state.sessions.begin() as db:
        row=db.get(Leave,str(lid))
        if not row:
            raise HTTPException(404,'找不到此申請。')
        if row.employee_id==user.employee_id:
            raise HTTPException(403,'自己的申請必須由另一位管理員審核。')
        db.execute(update(Employee).where(Employee.id==row.employee_id).values(name=Employee.name))
        db.refresh(row)
        previous=db.get(LeaveReview,row.id)
        if previous:
            if previous.decision==body.decision and previous.note==body.note and previous.reviewer_id==user.id:
                return serialize(row)
            raise HTTPException(409,'申請已由管理員處理，請重新整理。')
        if row.status!='pending':
            raise HTTPException(409,'只能審核待審核的申請。')
        if body.decision=='rejected':
            db.execute(update(Balance).where(Balance.employee_id==row.employee_id,Balance.leave_type==row.leave_type).values(hours=Balance.hours+row.hours))
        row.status=body.decision
        db.add(LeaveReview(leave_id=row.id,reviewer_id=user.id,decision=body.decision,note=body.note))
        db.add(LeaveEvent(leave_id=row.id,employee_id=row.employee_id,action=body.decision,hours=row.hours))
        audit(db,user,'leave_'+body.decision,row.id)
        db.flush()
        return serialize(row)
