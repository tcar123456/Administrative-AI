"""Explicit, testable date and slot parsing for the offline demo, not an LLM."""
import re
from datetime import date, datetime, time, timedelta
from app.db import today

WEEKDAYS = {'一': 0, '二': 1, '三': 2, '四': 3, '五': 4, '六': 5, '日': 6, '天': 6}


def extract_day(text):
    relative = re.search(r'(下|這|本)(?:週|周)([一二三四五六日天])', text)
    explicit = re.search(r'(?:(\d{4})[-/年])?(\d{1,2})[-/月](\d{1,2})(?:日|號)?(?!\d)', text)
    if relative:
        return today() + timedelta(days=(7 if relative[1] == '下' else 0)-today().weekday()+WEEKDAYS[relative[2]])
    if explicit:
        try:
            return date(int(explicit[1] or today().year), int(explicit[2]), int(explicit[3]))
        except ValueError:
            raise ValueError('日期不存在，請重新輸入有效日期，例如 2026/10/02。')
    for keyword, delta in [('後天', 2), ('明天', 1), ('今天', 0)]:
        if keyword in text:
            return today() + timedelta(days=delta)
    return None


def leave_slots(text):
    result = {}
    core = re.split(r'原因[：:]', text, maxsplit=1)[0]
    if '特休' in core and '補休' in core:
        raise ValueError('請一次選擇一種假別：特休或補休。')
    if '補休' in core:
        result['leave_type'] = 'compensatory'
    elif '特休' in core:
        result['leave_type'] = 'annual'
    reason = re.search(r'原因[：:]\s*(.+)', text, re.S)
    if reason:
        result['reason'] = reason[1].strip()
    day = extract_day(core)
    if day:
        result['date'] = day.isoformat()
    clock = re.search(r'(\d{1,2}:\d{2})\s*(?:到|至|[-–~～])\s*(\d{1,2}:\d{2})', core)
    if clock:
        try:
            result['start'] = time.fromisoformat(clock[1].zfill(5)).isoformat()
            result['end'] = time.fromisoformat(clock[2].zfill(5)).isoformat()
        except ValueError:
            raise ValueError('時間格式不正確，請使用 09:00–12:00。')
    else:
        for keyword, hours in [('下午', (13, 18)), ('上午', (9, 12)), ('全天', (9, 18))]:
            if keyword in core:
                result.update(start=time(hours[0]).isoformat(), end=time(hours[1]).isoformat())
                break
    return result


def complete_leave(slots):
    labels = {'leave_type': '假別（特休／補休）', 'date': '日期', 'start': '時段（上午／下午／全天或 09:00–12:00）', 'reason': '原因（例如「原因：私人事務」）'}
    missing = [label for key, label in labels.items() if not slots.get(key)]
    if missing:
        raise ValueError('請補充' + '、'.join(missing) + '，我會接續準備這份草稿。')
    return dict(leave_type=slots['leave_type'], start_time=f"{slots['date']}T{slots['start']}", end_time=f"{slots['date']}T{slots['end']}", reason=slots['reason'])


def calendar_period(text):
    day = extract_day(text)
    if day:
        return day, day
    if '下週' in text or '下周' in text:
        start = today() + timedelta(days=7-today().weekday())
        return start, start + timedelta(days=6)
    if any(word in text for word in ['本週', '這週', '本周', '這周']):
        start = today() - timedelta(days=today().weekday())
        return start, start + timedelta(days=6)
    return today(), today() + timedelta(days=6)
