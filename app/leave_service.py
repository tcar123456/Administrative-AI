from datetime import datetime, time, timedelta
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from app.db import Balance, Employee, Leave, LeaveEvent, today, TAIPEI

LABELS = {'annual': '特休', 'compensatory': '補休'}


class LeaveInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    leave_type: Literal['annual', 'compensatory']
    start_time: datetime
    end_time: datetime
    reason: str = Field(min_length=1, max_length=200)


def local(dt):
    return dt.astimezone(TAIPEI).replace(tzinfo=None) if dt.tzinfo else dt


def validate_period(start, end):
    start, end = local(start), local(end)
    if start.date() < today() or start <= datetime.now(TAIPEI).replace(tzinfo=None):
        raise ValueError('請選擇未來的工作時段。')
    if start.date() > today() + timedelta(days=365):
        raise ValueError('僅接受未來一年內的申請。')
    if start.date() != end.date() or end <= start or start.weekday() >= 5:
        raise ValueError('目前支援同一工作日（週一至週五）的請假。')
    if any(dt.minute not in (0, 30) or dt.second or dt.microsecond for dt in (start, end)):
        raise ValueError('請以 30 分鐘為單位申請。')
    if start.time() < time(9) or end.time() > time(18) or time(12) <= start.time() < time(13) or time(12) < end.time() <= time(13):
        raise ValueError('工作時段為 09:00–12:00、13:00–18:00。')
    hours = (end - start).total_seconds() / 3600
    if start.time() < time(12) and end.time() > time(13):
        hours -= 1
    return start, end, hours


def balance_data(db, eid):
    return [{'leave_type': b.leave_type, 'label': LABELS[b.leave_type], 'hours': b.hours, 'days': b.hours / 8} for b in db.scalars(select(Balance).where(Balance.employee_id == eid))]


def serialize(row):
    return dict(id=row.id, leave_type=row.leave_type, label=LABELS[row.leave_type], start=row.start.isoformat(), end=row.end.isoformat(), hours=row.hours, reason=row.reason, status=row.status)


def check_available(db, eid, kind, start, end, hours):
    account = db.scalar(select(Balance).where(Balance.employee_id == eid, Balance.leave_type == kind))
    if not account or account.hours < hours:
        raise ValueError(f'{LABELS[kind]}餘額不足，無法申請 {hours:g} 小時。請查詢其他假別餘額。')
    overlap = db.scalar(select(Leave).where(Leave.employee_id == eid, Leave.status.in_(['pending', 'approved']), Leave.start < end, Leave.end > start))
    if overlap:
        raise ValueError('此時段與已提交的請假申請重疊。')


def draft_leave(db, eid, args):
    db.execute(update(Employee).where(Employee.id == eid).values(name=Employee.name))
    start, end, hours = validate_period(args.start_time, args.end_time)
    check_available(db, eid, args.leave_type, start, end, hours)
    existing = db.scalar(select(Leave).where(Leave.employee_id == eid, Leave.status == 'draft', Leave.leave_type == args.leave_type, Leave.start == start, Leave.end == end, Leave.reason == args.reason))
    if existing:
        return serialize(existing)
    row = Leave(employee_id=eid, leave_type=args.leave_type, start=start, end=end, hours=hours, reason=args.reason)
    db.add(row)
    db.flush()
    db.add(LeaveEvent(leave_id=row.id, employee_id=eid, action='draft_created', hours=hours))
    return serialize(row)


def confirm_leave(db, eid, leave_id):
    # Serialize all leave mutations per employee, including overlapping different leave types.
    db.execute(update(Employee).where(Employee.id == eid).values(name=Employee.name))
    row = db.scalar(select(Leave).where(Leave.id == leave_id, Leave.employee_id == eid).execution_options(populate_existing=True))
    if not row:
        raise LookupError('找不到此申請。')
    if row.status == 'pending':
        return serialize(row)
    if row.status != 'draft':
        raise ValueError('此申請已取消或撤回，無法再次送出。')
    start, end, hours = validate_period(row.start, row.end)
    check_available(db, eid, row.leave_type, start, end, hours)
    changed = db.execute(update(Balance).where(Balance.employee_id == eid, Balance.leave_type == row.leave_type, Balance.hours >= hours).values(hours=Balance.hours-hours))
    if changed.rowcount != 1:
        raise ValueError('餘額已變更，請重新查詢。')
    row.status = 'pending'
    db.add(LeaveEvent(leave_id=row.id, employee_id=eid, action='submitted', hours=hours))
    db.flush()
    return serialize(row)


def withdraw_leave(db, eid, leave_id):
    db.execute(update(Employee).where(Employee.id == eid).values(name=Employee.name))
    row = db.scalar(select(Leave).where(Leave.id == leave_id, Leave.employee_id == eid).execution_options(populate_existing=True))
    if not row:
        raise LookupError('找不到此申請。')
    if row.status == 'withdrawn':
        return serialize(row)
    if row.status != 'pending':
        raise ValueError('只能撤回待審核的申請。')
    if row.start <= datetime.now(TAIPEI).replace(tzinfo=None):
        raise ValueError('請假時段已開始，請聯絡人資處理。')
    db.execute(update(Balance).where(Balance.employee_id == eid, Balance.leave_type == row.leave_type).values(hours=Balance.hours+row.hours))
    row.status = 'withdrawn'
    db.add(LeaveEvent(leave_id=row.id, employee_id=eid, action='withdrawn', hours=row.hours))
    db.flush()
    return serialize(row)
