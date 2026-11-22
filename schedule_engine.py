"""Calendar semantics for PULS. ISO odd/even weeks; date exceptions never rewrite recurring entries.

Pure Python + sqlite3, independently testable without Flask.
"""
from datetime import date, datetime, timedelta
from lesson_meta import LESSON_TYPES, load_items

RUS_MONTHS = ('января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря')
DAYS_SHORT = ('Пн','Вт','Ср','Чт','Пт','Сб','Вс')


def week_start(day):
    return day - timedelta(days=day.weekday())


def week_kind(day):
    return 'even' if day.isocalendar().week % 2 == 0 else 'odd'


def month_title(day):
    return f'{day.day} {RUS_MONTHS[day.month - 1]}'


def week_title(start):
    end = start + timedelta(days=6)
    return f'{month_title(start)} — {month_title(end)} {end.year}'


def resolve_date(value, today=None):
    fallback = today or date.today()
    try:
        selected = date.fromisoformat(value) if value else fallback
    except (ValueError, TypeError):
        selected = fallback
    # Do not allow distant or nonsensical dates from GET params.
    if abs((selected - fallback).days) > 730:
        selected = fallback
    return selected


def _session_metadata(conn, item, group, lesson_date, is_override=False):
    """Expand actual session members (not decorative labels)."""
    kind = item['lesson_type'] if 'lesson_type' in item.keys() else 'ПР'
    teachers = load_items(item['teachers_json'] if 'teachers_json' in item.keys() else None, item['teacher'])
    rooms = load_items(item['rooms_json'] if 'rooms_json' in item.keys() else None, item['room'])
    shared_id = item['shared_id'] if 'shared_id' in item.keys() else None
    groups = [group]
    if shared_id:
        if is_override:
            peers = conn.execute("""SELECT group_name FROM schedule_overrides WHERE shared_id=?
                AND date=? AND slot_id=? ORDER BY group_name""",
                (shared_id, lesson_date, item['slot_id'])).fetchall()
        else:
            peers = conn.execute("""SELECT l.group_name FROM group_lessons l
                WHERE l.shared_id=? AND l.weekday=? AND l.slot_id=?
                AND l.week_type IN ('both',?) AND NOT EXISTS (
                  SELECT 1 FROM schedule_overrides o WHERE o.group_name=l.group_name
                  AND o.date=? AND o.slot_id=l.slot_id)
                ORDER BY l.group_name""",
                (shared_id, item['weekday'], item['slot_id'],
                 week_kind(date.fromisoformat(lesson_date)), lesson_date)).fetchall()
        groups = [r['group_name'] for r in peers] or [group]
    return {'lesson_type': kind if kind in LESSON_TYPES else 'ПР',
            'lesson_type_name': LESSON_TYPES.get(kind, 'Практическое занятие'),
            'teachers': teachers, 'rooms': rooms, 'groups': groups}


def week_events(conn, group_name, week_date):
    """Seven dated columns. Each item is a plain dict to avoid sqlite.Row mutation traps.

    A regular lesson applies on both weeks or its ISO parity. A dated exception takes
    precedence only on that actual date and slot. Cancellation stays visible.
    """
    monday = week_start(week_date)
    parity = week_kind(monday)
    base = conn.execute('''SELECT l.*,s.number AS slot_number, s.starts AS slot_starts,s.ends AS slot_ends
        FROM group_lessons l LEFT JOIN bell_slots s ON s.id=l.slot_id
        WHERE l.group_name=? AND l.week_type IN ('both',?) ORDER BY l.weekday,COALESCE(s.number,99),l.starts''',
        (group_name,parity)).fetchall()
    overrides = conn.execute('''SELECT o.*,s.number AS slot_number,s.starts AS slot_starts,s.ends AS slot_ends
        FROM schedule_overrides o LEFT JOIN bell_slots s ON s.id=o.slot_id
        WHERE o.group_name=? AND o.date>=? AND o.date<=? ORDER BY o.date,slot_number''',
        (group_name,monday.isoformat(),(monday+timedelta(days=6)).isoformat())).fetchall()
    by_date = {}
    for event in base:
        d=(monday+timedelta(days=event['weekday'])).isoformat()
        key=(event['slot_id'], event['starts'])
        by_date.setdefault(d,{})[key] = {
            'id':event['id'],'date':d,'slot_id':event['slot_id'], 'slot_number':event['slot_number'],
            'starts':event['slot_starts'] or event['starts'],'ends':event['slot_ends'] or event['ends'],
            'subject':event['subject'],'teacher':event['teacher'],'room':event['room'],
            'changed':bool(event['changed']),'week_type':event['week_type'],
            'cancelled':False,'exception':False,'reason':'',
            **_session_metadata(conn,event,group_name,d),
        }
    for o in overrides:
        d=o['date']
        key=(o['slot_id'],o['slot_starts'])
        found=by_date.setdefault(d,{}).get(key)
        if o['action']=='cancel' and found:
            found.update(cancelled=True,changed=True,exception=True,reason=o['reason'],
                         groups=_session_metadata(conn,o,group_name,d,True)['groups'])
        elif o['action'] in ('replace','extra'):
            by_date[d][key]={
                'id':None,'date':d,'slot_id':o['slot_id'],'slot_number':o['slot_number'],
                'starts':o['slot_starts'],'ends':o['slot_ends'],
                'subject':o['subject'],'teacher':o['teacher'],'room':o['room'],
                'changed':True,'week_type':'date','cancelled':False,'exception':True,'reason':o['reason'],
                **_session_metadata(conn,o,group_name,d,True),
            }
    result=[]
    for n in range(7):
        day=monday+timedelta(days=n)
        lessons=list(by_date.get(day.isoformat(),{}).values())
        lessons.sort(key=lambda x:(x['starts'] or '',x['slot_number'] or 99))
        result.append(dict(date=day,day_index=n,label=DAYS_SHORT[n],lessons=lessons,
                           is_today=day==date.today(),is_selected=day==week_date,
                           active_count=sum(not x['cancelled'] for x in lessons)))
    return result


def entries_for_editor(conn,group,kind):
    """Rows for a parity editor, including recurring entries shared with both weeks."""
    return conn.execute('''SELECT l.*,s.number AS slot_number,s.starts AS slot_starts,s.ends AS slot_ends
       FROM group_lessons l LEFT JOIN bell_slots s ON s.id=l.slot_id
       WHERE l.group_name=? AND l.week_type IN ('both',?)
       ORDER BY l.weekday, COALESCE(s.number,99)''',(group,kind)).fetchall()


def conflict(conn, group, weekday, slot_id, kind, exclude=None):
    """True when a slot would occur twice in the same group and same actual week."""
    rows=conn.execute('''SELECT id,week_type FROM group_lessons WHERE group_name=? AND weekday=? AND slot_id=?''',
                      (group,weekday,slot_id)).fetchall()
    return any(r['id']!=exclude and (kind=='both' or r['week_type']=='both' or r['week_type']==kind) for r in rows)
