import logging
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import timedelta
from threading import Lock
from uuid import UUID
from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, update, text
from sqlalchemy.exc import IntegrityError
from app.config import settings, ROOT
from app.db import engine, SessionLocal, seed, Employee, Leave, LeaveEvent, ChatReceipt, Conversation, today
from app.leave_service import LeaveInput, balance_data, draft_leave, confirm_leave, withdraw_leave, serialize
from app.agent import Runner, calendar_data
from app.rag import Knowledge
from app.security import current_user, read_session, vault, setup_secret, RateLimiter
from app.admin import router, default_id
from app.calendar import router as calendar_router
from app.db import AIProvider, LeaveReview
from app.demo_accounts import initialize_demo_admin
import secrets

log = logging.getLogger(__name__)
chat_lock = Lock()  # One worker: bound upstream spend and serialize chat transactions.


@asynccontextmanager
async def lifespan(app):
    settings.validate_runtime()
    seed(engine, settings.seed_demo)
    app.state.settings = settings
    app.state.sessions = SessionLocal
    app.state.vault = vault(settings)
    from cryptography.fernet import InvalidToken
    with SessionLocal() as db:
        try:
            for ciphertext in db.scalars(select(AIProvider.encrypted_key)):
                app.state.vault.decrypt(ciphertext.encode())
        except InvalidToken:
            raise ValueError('加密主密鑰與既有 AI 設定不符，請還原原本的 ENCRYPTION_KEY 或本機密鑰檔案。') from None
    app.state.limiter = RateLimiter()
    initialize_demo_admin(SessionLocal, settings)
    setup_secret(settings)
    app.state.kb = Knowledge(settings)
    try:
        app.state.kb.ingest(settings.knowledge_dir)
        yield
    finally:
        app.state.kb.close()


app = FastAPI(title='日常 Daywork · AI 行政助理', version='2.0.0', lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url='/api/openapi.json')
app.include_router(router)
app.include_router(calendar_router)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')




@app.exception_handler(IntegrityError)
async def conflict_error(request, exc):
    return JSONResponse(status_code=409, content={'detail':'資料已由另一個操作更新，請重新整理後重試。'})


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Pydantic's default error includes submitted values, which could contain credentials.
    return JSONResponse(status_code=422, content={'detail':'資料格式不正確，請檢查必填欄位、長度與格式。'})


@app.middleware('http')
async def protect(request: Request, call_next):
    from urllib.parse import urlparse
    allowed = {urlparse(settings.public_origin).netloc.lower()}
    if settings.environment == 'local':
        allowed |= {'localhost:8000', '127.0.0.1:8000'}
    if request.headers.get('host','').lower() not in allowed:
        return JSONResponse(status_code=400, content={'detail':'網站來源不正確。'})
    public = {'/api/health','/api/auth/status','/api/auth/login','/api/auth/setup'}
    is_api = request.url.path.startswith('/api/')
    unsafe = request.method not in ('GET','HEAD','OPTIONS')
    if is_api and unsafe:
        origin = request.headers.get('origin','').rstrip('/')
        allowed_origins = {settings.public_origin.rstrip('/')}
        if settings.environment == 'local':
            allowed_origins |= {'http://localhost:8000','http://127.0.0.1:8000'}
        if origin not in allowed_origins:
            return JSONResponse(status_code=403, content={'detail':'請從本站操作。'})
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content)>32768:
                return JSONResponse(status_code=413, content={'detail':'請求內容過大。'})
        request._body = bytes(content)
    if is_api and request.url.path not in public:
        from starlette.concurrency import run_in_threadpool
        def authenticate():
            with SessionLocal() as db:
                return read_session(db, request.cookies.get('daywork_session',''))
        user, session = await run_in_threadpool(authenticate)
        if not user:
            return JSONResponse(status_code=401, content={'detail':'登入已過期，請重新登入。'}, headers={'Cache-Control':'no-store'})
        if unsafe and not secrets.compare_digest(request.headers.get('x-csrf-token',''), session.csrf):
            return JSONResponse(status_code=403, content={'detail':'驗證已過期，請重新整理頁面。'})
        request.state.user, request.state.csrf = user, session.csrf
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if is_api or request.url.path == '/':
        response.headers['Cache-Control'] = 'no-store'
    if settings.environment == 'production':
        response.headers['Strict-Transport-Security'] = 'max-age=31536000'
    return response


@app.get('/')
def index():
    return FileResponse(ROOT / 'static/index.html')


@app.get('/api/health')
def health():
    checks = {'database': False, 'knowledge': False}
    try:
        with SessionLocal() as db:
            db.execute(text('SELECT 1'))
        checks['database'] = True
        checks['knowledge'] = app.state.kb.client.collection_exists(app.state.kb.collection)
    except Exception:
        log.exception('Readiness check failed')
    if not all(checks.values()):
        raise HTTPException(503, {'status': 'degraded', 'checks': checks})
    return {'status': 'ok', 'mode': settings.agent_mode, 'checks': checks}


@app.get('/api/dashboard')
def dashboard(user=Depends(current_user)):
    with SessionLocal() as db:
        employee = db.get(Employee, user.employee_id)
        return dict(employee={'id': employee.id, 'name': employee.name, 'department': employee.department}, today=today().isoformat(), mode=('configured' if db.scalar(select(AIProvider.id).where(AIProvider.enabled == True)) else (settings.agent_mode if settings.demo_available else 'unconfigured')), environment=settings.environment, seeded_demo=settings.seed_demo, demo_available=settings.demo_available, balances=balance_data(db, user.employee_id), leaves=[serialize(r) for r in db.scalars(select(Leave).where(Leave.employee_id == user.employee_id).order_by(Leave.start.desc()))], events=calendar_data(db, user.employee_id, today(), today()+timedelta(days=30)))


@app.get('/api/documents')
def documents(user=Depends(current_user)):
    return app.state.kb.documents()


class ChatInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    message: str = Field(min_length=1, max_length=2000)
    conversation_id: UUID | None = None
    request_id: UUID | None = None
    provider_id: UUID | None = None


def get_conversation(db, cid, user):
    row = db.get(Conversation, str(cid))
    if not row or row.employee_id != user.employee_id:
        raise HTTPException(404, '找不到此對話。')
    return row


@app.get('/api/conversations/{cid}')
def get_history(cid: UUID, user=Depends(current_user)):
    with SessionLocal() as db:
        row = get_conversation(db, cid, user)
        return {'conversation_id': row.id, 'messages': row.messages}


@app.get('/api/conversations')
def list_conversations(user=Depends(current_user)):
    with SessionLocal() as db:
        rows = list(db.scalars(select(Conversation).where(Conversation.employee_id == user.employee_id)))
        result = []
        for row in rows:
            if row.messages:
                result.append({'id': row.id, 'title': row.messages[0]['content'][:60], 'preview': row.messages[-1]['content'][:100], 'updated_at': row.messages[-1].get('at', ''), 'message_count': len(row.messages)})
        return sorted(result, key=lambda r: r['updated_at'], reverse=True)[:50]


@app.post('/api/chat')
def chat(body: ChatInput, user=Depends(current_user)):
    app.state.limiter.hit('chat:'+user.id, 20, 60)
    if not body.message.strip():
        raise HTTPException(422, '請輸入訊息。')
    if not chat_lock.acquire(blocking=False):
        raise HTTPException(409, '上一則訊息仍在處理，請稍候。')
    try:
        with SessionLocal.begin() as db:
            signature = hashlib.sha256(json.dumps({'message': body.message, 'conversation_id': str(body.conversation_id), 'provider_id': str(body.provider_id)}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if body.request_id:
                receipt = db.get(ChatReceipt, str(body.request_id))
                if receipt:
                    if receipt.employee_id != user.employee_id or receipt.signature != signature:
                        raise HTTPException(409, '此請求編號已用於不同訊息，請開始新的傳送。')
                    return receipt.response
            if body.conversation_id:
                conversation = get_conversation(db, body.conversation_id, user)
            else:
                conversation = Conversation(employee_id=user.employee_id, messages=[])
                db.add(conversation)
                db.flush()
            pid = str(body.provider_id) if body.provider_id else default_id(db)
            if not pid:
                pid = db.scalar(select(AIProvider.id).where(AIProvider.enabled == True, AIProvider.tested == True).order_by(AIProvider.name).limit(1))
            config = db.get(AIProvider, pid) if pid else None
            if pid and (not config or not config.enabled or not config.tested):
                raise HTTPException(409, '所選模型已停用或需要重新測試，請重新選擇。')
            if not config and not settings.demo_available:
                raise HTTPException(503, '尚未設定可用的 AI 模型，請聯絡管理員。')
            runner = Runner(db, user.employee_id, app.state.kb, settings)
            if config:
                runner.provider_config = (config.provider, config.model, app.state.vault.decrypt(config.encrypted_key.encode()).decode())
            result = runner.run(body.message, conversation.messages)
            from datetime import datetime
            from app.db import TAIPEI
            stamp = datetime.now(TAIPEI).isoformat()
            conversation.messages = (conversation.messages + [{'role': 'user', 'content': body.message, 'at': stamp}, {'role': 'assistant', 'content': result['answer'], 'result': result, 'at': stamp}])[-40:]
            reply = {**result, 'conversation_id': conversation.id}
            if body.request_id:
                db.add(ChatReceipt(id=str(body.request_id), employee_id=user.employee_id, signature=signature, response=reply))
            return reply
    except HTTPException:
        raise
    except Exception:
        log.warning('Agent request failed; transaction rolled back; upstream details suppressed')
        raise HTTPException(503, 'AI 或資料服務暫時無法連線，本次操作已回復。請稍後重試或檢查伺服器設定。')
    finally:
        chat_lock.release()


@app.post('/api/leaves/draft')
def create_draft(body: LeaveInput, user=Depends(current_user)):
    try:
        with SessionLocal.begin() as db:
            return draft_leave(db, user.employee_id, body)
    except ValueError as error:
        raise HTTPException(422, str(error))


@app.post('/api/leaves/{lid}/confirm')
def confirm(lid: UUID, user=Depends(current_user)):
    try:
        with SessionLocal.begin() as db:
            return confirm_leave(db, user.employee_id, str(lid))
    except LookupError as error:
        raise HTTPException(404, str(error))
    except ValueError as error:
        raise HTTPException(409, str(error))


@app.post('/api/leaves/{lid}/cancel')
def cancel(lid: UUID, user=Depends(current_user)):
    with SessionLocal.begin() as db:
        db.execute(update(Employee).where(Employee.id == user.employee_id).values(name=Employee.name))
        changed = db.execute(update(Leave).where(Leave.id == str(lid), Leave.employee_id == user.employee_id, Leave.status == 'draft').values(status='cancelled'))
        if changed.rowcount != 1:
            row = db.scalar(select(Leave).where(Leave.id == str(lid), Leave.employee_id == user.employee_id))
            if row and row.status == 'cancelled':
                return {'status': 'cancelled'}
            raise HTTPException(409, '草稿不存在或已送出。')
        row = db.get(Leave, str(lid))
        db.add(LeaveEvent(leave_id=row.id, employee_id=user.employee_id, action='cancelled', hours=row.hours))
        return {'status': 'cancelled'}


@app.post('/api/leaves/{lid}/withdraw')
def withdraw(lid: UUID, user=Depends(current_user)):
    try:
        with SessionLocal.begin() as db:
            return withdraw_leave(db, user.employee_id, str(lid))
    except LookupError as error:
        raise HTTPException(404, str(error))
    except ValueError as error:
        raise HTTPException(409, str(error))


@app.get('/api/leaves/{lid}/events')
def leave_events(lid: UUID, user=Depends(current_user)):
    with SessionLocal() as db:
        row = db.scalar(select(Leave).where(Leave.id == str(lid), Leave.employee_id == user.employee_id))
        if not row:
            raise HTTPException(404, '找不到此申請。')
        review = db.get(LeaveReview, row.id)
        return [{'note': review.note if review and e.action in ('approved','rejected') else '', 'action': e.action, 'hours': e.hours, 'at': e.occurred_at.isoformat()} for e in db.scalars(select(LeaveEvent).where(LeaveEvent.leave_id == row.id, LeaveEvent.employee_id == user.employee_id).order_by(LeaveEvent.occurred_at))]
