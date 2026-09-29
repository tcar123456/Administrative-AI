"""Personal calendar maintenance; the authenticated account owns every mutation."""
from datetime import date, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.db import CalendarEvent, today
from app.leave_service import local
from app.security import current_user

router = APIRouter(prefix='/api/calendar')


class EventInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=120)
    start: datetime
    end: datetime
    location: str = Field(default='', max_length=200)

    @model_validator(mode='after')
    def valid_period(self):
        self.start, self.end = local(self.start), local(self.end)
        if self.end <= self.start or self.end - self.start > timedelta(days=7):
            raise ValueError('行程結束時間必須晚於開始，且最長為七天。')
        return self


class NewEvent(EventInput):
    id: UUID = Field(default_factory=uuid4)


def event_data(row):
    return {key: getattr(row, key) for key in ('id', 'title', 'start', 'end', 'location')}


@router.get('')
def list_events(request: Request, start_date: date | None = None, end_date: date | None = None, user=Depends(current_user)):
    from app.agent import calendar_data
    start_date = start_date or today()
    end_date = end_date or start_date + timedelta(days=30)
    try:
        with request.app.state.sessions() as db:
            return calendar_data(db, user.employee_id, start_date, end_date)
    except ValueError as error:
        raise HTTPException(422, str(error))


@router.post('')
def create_event(body: NewEvent, request: Request, user=Depends(current_user)):
    with request.app.state.sessions.begin() as db:
        existing = db.get(CalendarEvent, str(body.id))
        values = body.model_dump(exclude={'id'})
        if existing:
            if existing.employee_id == user.employee_id and all(getattr(existing, k) == v for k, v in values.items()):
                return event_data(existing)
            raise HTTPException(409, '行程編號已使用，請重新開啟新增視窗。')
        row = CalendarEvent(id=str(body.id), employee_id=user.employee_id, **values)
        db.add(row)
        db.flush()
        return event_data(row)


@router.put('/{eid}')
def update_event(eid: UUID, body: EventInput, request: Request, user=Depends(current_user)):
    with request.app.state.sessions.begin() as db:
        row = db.scalar(select(CalendarEvent).where(CalendarEvent.id == str(eid), CalendarEvent.employee_id == user.employee_id).with_for_update())
        if not row:
            raise HTTPException(404, '找不到此行程。')
        for key, value in body.model_dump().items():
            setattr(row, key, value)
        db.flush()
        return event_data(row)


@router.delete('/{eid}')
def delete_event(eid: UUID, request: Request, user=Depends(current_user)):
    with request.app.state.sessions.begin() as db:
        row = db.scalar(select(CalendarEvent).where(CalendarEvent.id == str(eid), CalendarEvent.employee_id == user.employee_id).with_for_update())
        if not row:
            raise HTTPException(404, '找不到此行程。')
        db.delete(row)
        return {'ok': True}
