"""PULS Campus OS — self-contained contest prototype, all user data fictional."""
import io
import csv
import json
import hashlib
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from functools import wraps
from html import escape as html_escape

from flask import (Flask, abort, flash, g, redirect, render_template,
                   request, send_file, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash
from schedule_engine import week_start, week_kind, week_events, week_title, resolve_date, conflict, entries_for_editor, month_title
from import_helpers import parse_registry
from lesson_meta import LESSON_TYPES, metadata_from_form, load_items

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get('SECRET_KEY') or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
    SESSION_COOKIE_SECURE=os.environ.get('HTTPS_ONLY') == '1',
    MAX_CONTENT_LENGTH=3 * 1024 * 1024,
)
DB = os.environ.get('DATABASE_PATH') or os.path.join(BASE, 'campus.db')
DEMO_MODE = os.environ.get('DEMO_MODE', '1') == '1'
DAYS = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
DOC_TYPES = ('Справка об обучении', 'Справка для военкомата')
REQUEST_TYPES = DOC_TYPES + ('Заявление в деканат', 'Другой вопрос')
TERMINAL = ('Готова', 'Отклонена', 'Отменена')
NOW = lambda: datetime.now().strftime('%Y-%m-%d %H:%M')


def db():
    if 'db' not in g:
        g.db = sqlite3.connect(DB, timeout=15)
        g.db.row_factory = sqlite3.Row
        g.db.execute('PRAGMA foreign_keys=ON')
        g.db.execute('PRAGMA busy_timeout=15000')
    return g.db


@app.teardown_appcontext
def close_db(exc):
    connection = g.pop('db', None)
    if connection:
        connection.close()


def column_if_missing(conn, table, column, definition):
    if column not in {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')


def init_db():
    """Idempotent creation + migration of v3 data; never overwrite real entries."""
    os.makedirs(os.path.dirname(os.path.abspath(DB)), exist_ok=True)
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
      password_hash TEXT NOT NULL, full_name TEXT NOT NULL, faculty TEXT NOT NULL,
      program TEXT NOT NULL, course INTEGER NOT NULL, group_name TEXT NOT NULL,
      role TEXT NOT NULL DEFAULT 'student');
    CREATE TABLE IF NOT EXISTS lessons (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
      weekday INTEGER NOT NULL, starts TEXT NOT NULL, ends TEXT NOT NULL, subject TEXT NOT NULL,
      teacher TEXT NOT NULL, room TEXT NOT NULL, changed INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS grades (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
      subject TEXT NOT NULL, grade TEXT NOT NULL, kind TEXT NOT NULL, date TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
      type TEXT NOT NULL, comment TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Принята', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS notices (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
      title TEXT NOT NULL, body TEXT NOT NULL, date TEXT NOT NULL, kind TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS request_history (id INTEGER PRIMARY KEY,
      request_id INTEGER NOT NULL REFERENCES requests(id), status TEXT NOT NULL,
      note TEXT NOT NULL DEFAULT '', changed_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS notice_reads (user_id INTEGER NOT NULL REFERENCES users(id),
      notice_id INTEGER NOT NULL REFERENCES notices(id), PRIMARY KEY(user_id,notice_id));
    CREATE TABLE IF NOT EXISTS academic_groups (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
      faculty TEXT NOT NULL, program TEXT NOT NULL, course INTEGER NOT NULL CHECK(course BETWEEN 1 AND 6));
    CREATE TABLE IF NOT EXISTS audit_log (id INTEGER PRIMARY KEY, actor_id INTEGER REFERENCES users(id),
      action TEXT NOT NULL, target TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS group_lessons (id INTEGER PRIMARY KEY,
      group_name TEXT NOT NULL REFERENCES academic_groups(name), weekday INTEGER NOT NULL CHECK(weekday BETWEEN 0 AND 6),
      starts TEXT NOT NULL, ends TEXT NOT NULL, subject TEXT NOT NULL, teacher TEXT NOT NULL,
      room TEXT NOT NULL, changed INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS request_documents (id INTEGER PRIMARY KEY,
      request_id INTEGER NOT NULL UNIQUE REFERENCES requests(id), filename TEXT NOT NULL,
      content BLOB NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS request_messages (id INTEGER PRIMARY KEY,
      request_id INTEGER NOT NULL REFERENCES requests(id), author_id INTEGER NOT NULL REFERENCES users(id),
      body TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_group_lessons ON group_lessons(group_name,weekday,starts);
    CREATE INDEX IF NOT EXISTS idx_request_student ON requests(user_id,id);
    CREATE INDEX IF NOT EXISTS idx_notice_student ON notices(user_id,id);
    ''')
    column_if_missing(conn, 'users', 'role', "TEXT NOT NULL DEFAULT 'student'")
    column_if_missing(conn, 'users', 'is_active', 'INTEGER NOT NULL DEFAULT 1')
    column_if_missing(conn, 'users', 'must_change_password', 'INTEGER NOT NULL DEFAULT 0')
    column_if_missing(conn, 'requests', 'result_text', "TEXT NOT NULL DEFAULT ''")
    column_if_missing(conn, 'requests', 'resolved_at', "TEXT NOT NULL DEFAULT ''")
    column_if_missing(conn, 'request_history', 'actor_id', 'INTEGER REFERENCES users(id)')
    column_if_missing(conn, 'notices', 'request_id', 'INTEGER REFERENCES requests(id)')
    # v5: recurrent timetables, ISO week parity, date-specific exceptions, reusable bell grid.
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS bell_slots (id INTEGER PRIMARY KEY, number INTEGER UNIQUE NOT NULL,
      starts TEXT NOT NULL, ends TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS schedule_overrides (id INTEGER PRIMARY KEY,
      group_name TEXT NOT NULL REFERENCES academic_groups(name), date TEXT NOT NULL,
      slot_id INTEGER NOT NULL REFERENCES bell_slots(id), action TEXT NOT NULL,
      subject TEXT NOT NULL DEFAULT '',teacher TEXT NOT NULL DEFAULT '',room TEXT NOT NULL DEFAULT '',
      reason TEXT NOT NULL DEFAULT '', UNIQUE(group_name,date,slot_id));
    CREATE TABLE IF NOT EXISTS import_batches (id INTEGER PRIMARY KEY,
      actor_id INTEGER REFERENCES users(id), created_at TEXT NOT NULL,
      payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'preview');
    CREATE TABLE IF NOT EXISTS account_activations (user_id INTEGER PRIMARY KEY REFERENCES users(id),
      token_hash TEXT NOT NULL UNIQUE, expires TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_overrides_date ON schedule_overrides(group_name,date);
    ''')
    # v6: type, many teachers/rooms, and shared sessions spanning multiple groups.
    for table in ('group_lessons', 'schedule_overrides'):
        column_if_missing(conn, table, 'lesson_type', "TEXT NOT NULL DEFAULT 'ПР'")
        column_if_missing(conn, table, 'teachers_json', "TEXT NOT NULL DEFAULT '[]'")
        column_if_missing(conn, table, 'rooms_json', "TEXT NOT NULL DEFAULT '[]'")
        column_if_missing(conn, table, 'shared_id', 'TEXT')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_shared_lesson ON group_lessons(shared_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_shared_override ON schedule_overrides(shared_id)')
    column_if_missing(conn, 'group_lessons', 'week_type', "TEXT NOT NULL DEFAULT 'both'")
    column_if_missing(conn, 'group_lessons', 'slot_id', 'INTEGER REFERENCES bell_slots(id)')
    column_if_missing(conn, 'notices', 'target_date', "TEXT NOT NULL DEFAULT ''")
    column_if_missing(conn, 'grades', 'period', "TEXT NOT NULL DEFAULT '2025/26 · весна'")
    if not conn.execute('SELECT 1 FROM bell_slots').fetchone():
        for number, starts, ends in [(1,'09:00','10:30'),(2,'10:45','12:15'),(3,'12:30','14:00'),
                                      (4,'14:15','15:45'),(5,'16:00','17:30'),(6,'17:45','19:15')]:
            conn.execute('INSERT INTO bell_slots(number,starts,ends) VALUES(?,?,?)',(number,starts,ends))

    fresh_demo = not conn.execute("SELECT 1 FROM users WHERE role='student' LIMIT 1").fetchone()
    if not conn.execute("SELECT 1 FROM users WHERE role='student' LIMIT 1").fetchone():
        demo_students = [
            ('student1', 'Александра Миронова', 'Факультет цифровых технологий', 'Бизнес-информатика', 2, 'БИ-24-1'),
            ('student2', 'Марк Белозёров', 'Факультет управления', 'Менеджмент', 3, 'МН-23-2'),
        ]
        for index, record in enumerate(demo_students):
            username, full_name, faculty, program, course, group_name = record
            uid = conn.execute('''INSERT INTO users(username,password_hash,full_name,faculty,program,course,group_name,role)
              VALUES(?,?,?,?,?,?,?,?)''', (username, generate_password_hash('1234'), full_name, faculty,
                                     program, course, group_name, 'student')).lastrowid
            subjects = (['Цифровая экономика', 'Проектный менеджмент', 'Аналитика данных',
                         'Английский язык', 'Дизайн-мышление'] if index == 0 else
                        ['Стратегический менеджмент', 'Маркетинг', 'Финансы',
                         'Деловой английский', 'Управление командой'])
            for day in range(5):
                for j in range(2 + (day % 2)):
                    conn.execute('''INSERT INTO lessons(user_id,weekday,starts,ends,subject,teacher,room,changed)
                      VALUES(?,?,?,?,?,?,?,?)''', (uid, day, ['09:00','10:45','12:30'][j],
                        ['10:30','12:15','14:00'][j], subjects[(day+j)%5],
                        ['Е. А. Смирнова','Д. В. Орлов','Н. П. Волкова'][j], str(305+day*10+j), int(day==2 and j==1)))
            for j, subject in enumerate(subjects):
                conn.execute('INSERT INTO grades(user_id,subject,grade,kind,date) VALUES(?,?,?,?,?)',
                     (uid,subject,(['5','4','5','Зачёт','4'] if index==0 else ['4','5','4','Зачёт','5'])[j],
                      'Зачёт' if j==3 else 'Экзамен',f'2026-11-{10+j:02d}'))
            conn.execute('INSERT INTO notices(user_id,title,body,date,kind) VALUES(?,?,?,?,?)',
                         (uid,'Обновление расписания','Изменения занятий отображаются в расписании группы.','2026-11-20','schedule'))
            conn.execute('INSERT INTO notices(user_id,title,body,date,kind) VALUES(?,?,?,?,?)',
                         (uid,'Проектная неделя','Проверьте объявления учебного офиса.','2026-11-19','news'))
        rid=conn.execute('INSERT INTO requests(user_id,type,comment,status,created_at) VALUES(?,?,?,?,?)',
                    (1,'Справка об обучении','Для предоставления по месту требования','В работе','2026-11-18 14:20')).lastrowid
        conn.execute('INSERT INTO request_history(request_id,status,note,changed_at) VALUES(?,?,?,?)',
                     (rid,'В работе','Сотрудник начал обработку запроса','2026-11-18 14:35'))

    if not conn.execute("SELECT 1 FROM users WHERE username='dean'").fetchone():
        conn.execute('''INSERT INTO users(username,password_hash,full_name,faculty,program,course,group_name,role)
          VALUES(?,?,?,?,?,?,?,?)''', ('dean',generate_password_hash('1234'),'Елена Соколова',
                                    'Учебный офис','Сотрудник',0,'Деканат','staff'))
    if not conn.execute("SELECT 1 FROM users WHERE username='admin'").fetchone():
        conn.execute('''INSERT INTO users(username,password_hash,full_name,faculty,program,course,group_name,role)
          VALUES(?,?,?,?,?,?,?,?)''', ('admin',generate_password_hash('1234'),'Администратор ПУЛЬС',
                                    'Администрация','Администрирование',0,'Администрация','admin'))
    # Upgrade only the exact original v1/v3 demo password; preserve user-changed credentials.
    if DEMO_MODE:
        for demo_name in ('student1','student2'):
            seeded=conn.execute('SELECT password_hash FROM users WHERE username=?',(demo_name,)).fetchone()
            if seeded and check_password_hash(seeded['password_hash'],'Pulse2026!'):
                conn.execute('UPDATE users SET password_hash=? WHERE username=?',
                             (generate_password_hash('1234'),demo_name))
    conn.execute("""INSERT OR IGNORE INTO academic_groups(name,faculty,program,course)
      SELECT DISTINCT group_name,faculty,program,course FROM users WHERE role='student'""")
    # Migration: group timetable is canonical. New students inherit it immediately.
    for group in conn.execute('SELECT name FROM academic_groups').fetchall():
        if conn.execute('SELECT 1 FROM group_lessons WHERE group_name=? LIMIT 1', (group['name'],)).fetchone():
            continue
        representative = conn.execute('''SELECT u.id FROM users u JOIN lessons l ON l.user_id=u.id
          WHERE u.group_name=? AND u.role='student' ORDER BY u.id LIMIT 1''', (group['name'],)).fetchone()
        if representative:
            conn.execute('''INSERT INTO group_lessons(group_name,weekday,starts,ends,subject,teacher,room,changed)
              SELECT ?,weekday,starts,ends,subject,teacher,room,changed FROM lessons WHERE user_id=?''',
              (group['name'], representative['id']))
    for r in conn.execute('SELECT id,starts,ends FROM group_lessons WHERE slot_id IS NULL').fetchall():
        slot=conn.execute('SELECT id FROM bell_slots WHERE starts=? AND ends=?',(r['starts'],r['ends'])).fetchone()
        if not slot:
            number=conn.execute('SELECT COALESCE(MAX(number),0)+1 FROM bell_slots').fetchone()[0]
            sid=conn.execute('INSERT INTO bell_slots(number,starts,ends) VALUES(?,?,?)',
                (number,r['starts'],r['ends'])).lastrowid
        else: sid=slot['id']
        conn.execute('UPDATE group_lessons SET slot_id=? WHERE id=?',(sid,r['id']))
    # The v4 demo 'exams' cannot be November finals of an ongoing autumn semester:
    # relabel only untouched synthetic seed rows as last spring's completed attestations.
    conn.execute('''UPDATE grades SET date=replace(date,'2026-11-','2026-06-')
      WHERE date BETWEEN '2026-11-10' AND '2026-11-14' AND period='2025/26 · весна'
      AND user_id IN (SELECT id FROM users WHERE username IN ('student1','student2'))''')


    # A freshly generated demo should illustrate a multi-teacher lecture, not a fake
    # result imported from a real university. Existing users' schedules are untouched.
    if fresh_demo:
        example = conn.execute("""SELECT id FROM group_lessons WHERE group_name='БИ-24-1'
            AND weekday=0 AND starts='09:00' LIMIT 1""").fetchone()
        if example:
            conn.execute("""UPDATE group_lessons SET subject=?,lesson_type='ЛК',teacher=?,room=?,
              teachers_json=?,rooms_json=? WHERE id=?""",
              ('Системы искусственного интеллекта и большие данные',
               'Р. Г. Большин','А-13',
               json.dumps(['Р. Г. Большин','С. Р. Грушина'],ensure_ascii=False),
               json.dumps(['А-13','В-78'],ensure_ascii=False),example['id']))
            # Illustration of a lecture shared by three fictional cohorts. It is a
            # real linked schedule record for each group, not fake static tags.
            token=secrets.token_hex(12)
            conn.execute('UPDATE group_lessons SET shared_id=? WHERE id=?',(token,example['id']))
            lecture=conn.execute('SELECT * FROM group_lessons WHERE id=?',(example['id'],)).fetchone()
            for peer in ('БИ-24-2','БИ-24-3'):
                conn.execute('''INSERT OR IGNORE INTO academic_groups(name,faculty,program,course)
                   VALUES(?,?,?,?)''',(peer,'Факультет цифровых технологий','Бизнес-информатика',2))
                conn.execute('''INSERT INTO group_lessons(group_name,weekday,starts,ends,subject,teacher,room,
                    changed,slot_id,week_type,lesson_type,teachers_json,rooms_json,shared_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                    (peer,lecture['weekday'],lecture['starts'],lecture['ends'],lecture['subject'],
                     lecture['teacher'],lecture['room'],lecture['changed'],lecture['slot_id'],
                     'both',lecture['lesson_type'],lecture['teachers_json'],lecture['rooms_json'],token))

    # Reject accidental duplicate weekly entries even under concurrent writers.
    conn.execute('''CREATE UNIQUE INDEX IF NOT EXISTS idx_lessons_unique_template
      ON group_lessons(group_name,weekday,slot_id,week_type)''')
    # A legacy 'done' status without a deliverable is not a fulfilled service.
    unfulfilled = conn.execute("""SELECT id FROM requests WHERE status='Готова' AND result_text=''
         AND NOT EXISTS (SELECT 1 FROM request_documents d WHERE d.request_id=requests.id)""").fetchall()
    for old in unfulfilled:
        conn.execute("UPDATE requests SET status='В работе' WHERE id=?", (old['id'],))
        conn.execute('INSERT INTO request_history(request_id,status,note,changed_at) VALUES(?,?,?,?)',
                     (old['id'],'В работе','Не было результата выдачи; заявка возвращена в работу',NOW()))
    conn.commit()
    conn.close()


def current_user():
    if not session.get('uid'):
        return None
    return db().execute('SELECT * FROM users WHERE id=? AND is_active=1', (session['uid'],)).fetchone()


@app.before_request
def check_session_and_csrf():
    if '_csrf' not in session:
        session['_csrf'] = secrets.token_urlsafe(32)
    if request.method == 'POST' and not secrets.compare_digest(request.form.get('_csrf', ''), session['_csrf']):
        abort(400)
    if session.get('uid') and not current_user():
        session.clear()
        session['_csrf'] = secrets.token_urlsafe(32)
        if request.endpoint not in ('login','demo_login','static'):
            return redirect(url_for('login'))
    if session.get('uid') and request.endpoint not in ('profile','change_password','logout','static'):
        u=current_user()
        if u and u['must_change_password']:
            return redirect(url_for('profile'))


@app.after_request
def add_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Cache-Control'] = 'no-store' if session.get('uid') else 'no-cache'
    return response


@app.context_processor
def inject_context():
    u=current_user()
    unread=0
    recent=[]
    if u and u['role']=='student':
        unread=db().execute('''SELECT COUNT(*) FROM notices n WHERE n.user_id=? AND NOT EXISTS
            (SELECT 1 FROM notice_reads nr WHERE nr.user_id=? AND nr.notice_id=n.id)''',
            (u['id'],u['id'])).fetchone()[0]
        recent=db().execute('''SELECT n.*, nr.notice_id IS NOT NULL AS is_read FROM notices n
            LEFT JOIN notice_reads nr ON nr.user_id=? AND nr.notice_id=n.id
            WHERE n.user_id=? ORDER BY n.id DESC LIMIT 5''',(u['id'],u['id'])).fetchall()
    return dict(me=u,days=DAYS,today=date.today(),active_page=request.endpoint,
                demo_mode=DEMO_MODE,doc_types=DOC_TYPES,unread_count=unread,recent_notices=recent)


def allowed_role(role):
    def decorator(function):
        @wraps(function)
        def inner(*args,**kwargs):
            u=current_user()
            if not u:
                return redirect(url_for('login'))
            if role is not None and u['role'] != role:
                abort(403)
            return function(*args,**kwargs)
        return inner
    return decorator

student_required = allowed_role('student')
staff_required = allowed_role('staff')
admin_required = allowed_role('admin')
login_required = allowed_role(None)


def audit(action,target):
    db().execute('INSERT INTO audit_log(actor_id,action,target,created_at) VALUES(?,?,?,?)',
                 (session['uid'], action, target, NOW()))


def destination(role):
    return 'admin_dashboard' if role=='admin' else 'staff_dashboard' if role=='staff' else 'home'


def notice(uid, title, body, kind='request', rid=None, target_date=''):
    db().execute('INSERT INTO notices(user_id,title,body,date,kind,request_id,target_date) VALUES(?,?,?,?,?,?,?)',
                 (uid,title,body,NOW(),kind,rid,target_date))


def record_history(rid, status, note):
    db().execute('''INSERT INTO request_history(request_id,status,note,changed_at,actor_id)
       VALUES(?,?,?,?,?)''', (rid,status,note,NOW(),session.get('uid')))


def get_request_or_404(rid):
    row=db().execute('''SELECT r.*,u.full_name,u.group_name,u.faculty,u.program,u.course,u.id AS student_id
      FROM requests r JOIN users u ON u.id=r.user_id WHERE r.id=?''',(rid,)).fetchone()
    if not row:
        abort(404)
    u=current_user()
    if u['role']=='student' and row['user_id']!=u['id']:
        abort(404)
    if u['role'] not in ('student','staff'):
        abort(403)
    return row


@app.route('/login',methods=['GET','POST'])
def login():
    if request.method=='POST':
        u=db().execute('SELECT * FROM users WHERE username=?',(request.form.get('username','').strip().lower(),)).fetchone()
        if u and u['is_active'] and check_password_hash(u['password_hash'],request.form.get('password','')):
            session.clear(); session['uid']=u['id']; session['_csrf']=secrets.token_urlsafe(32)
            return redirect(url_for(destination(u['role'])))
        flash('Неверный логин или пароль','error')
    return render_template('login.html')


@app.route('/demo-login',methods=['POST'])
def demo_login():
    if not DEMO_MODE:
        abort(404)
    username=request.form.get('account','')
    if username not in ('student1','student2','dean','admin'):
        abort(400)
    u=db().execute('SELECT id,role FROM users WHERE username=? AND is_active=1',(username,)).fetchone()
    if not u:
        abort(404)
    session.clear(); session['uid']=u['id']; session['_csrf']=secrets.token_urlsafe(32)
    return redirect(url_for(destination(u['role'])))


@app.route('/logout',methods=['POST'])
@login_required
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@student_required
def home():
    u=current_user()
    now=date.today()
    current=week_events(db(),u['group_name'],now)
    today_lessons=current[now.weekday()]['lessons']
    notices=db().execute('SELECT * FROM notices WHERE user_id=? ORDER BY id DESC LIMIT 3',(u['id'],)).fetchall()
    count=db().execute("SELECT COUNT(*) FROM requests WHERE user_id=? AND status NOT IN ('Готова','Отклонена','Отменена')",(u['id'],)).fetchone()[0]
    action=db().execute("SELECT * FROM requests WHERE user_id=? AND status='Требует уточнения' ORDER BY id DESC LIMIT 1",(u['id'],)).fetchone()
    ready=db().execute("""SELECT r.* FROM requests r JOIN request_documents d ON d.request_id=r.id
      WHERE r.user_id=? AND r.status='Готова' ORDER BY r.id DESC LIMIT 1""",(u['id'],)).fetchone()
    upcoming=[]
    for offset in (0,7):
        for column in week_events(db(),u['group_name'],now+timedelta(days=offset)):
            if column['date']>=now:
                upcoming.extend([(column['date'],r) for r in column['lessons'] if not r['cancelled']])
    upcoming.sort(key=lambda row:(row[0],row[1]['starts']))
    next_lesson=upcoming[0] if upcoming else None
    return render_template('home.html',lessons=[x for x in today_lessons if not x['cancelled']],notices=notices,
                           request_count=count,weekday=now.weekday(),action=action,ready=ready,
                           next_lesson=next_lesson,calendar_date=now,parity=week_kind(now))

@app.route('/schedule')
@student_required
def schedule():
    selected=resolve_date(request.args.get('date'))
    if 'date' not in request.args and 'day' in request.args:
        try:
            weekday=int(request.args.get('day','0'))
            if 0<=weekday<=6: selected=week_start(selected)+timedelta(days=weekday)
        except ValueError: pass
    monday=week_start(selected)
    columns=week_events(db(),current_user()['group_name'],selected)
    return render_template('schedule.html',columns=columns,selected=selected,week_start=monday,
        week_title=week_title(monday),parity=week_kind(monday),
        previous=(monday-timedelta(days=7)).isoformat(),upcoming=(monday+timedelta(days=7)).isoformat(),
        total=sum(x['active_count'] for x in columns))

@app.route('/grades')
@student_required
def grades():
    current_period='2026/27 · осень'
    rows=db().execute('SELECT * FROM grades WHERE user_id=? ORDER BY date DESC,subject',(session['uid'],)).fetchall()
    ongoing=[g for g in rows if g['period']==current_period]
    archive=[g for g in rows if g['period']!=current_period]
    numeric=[int(x['grade']) for x in archive if x['grade'].isdigit()]
    average=round(sum(numeric)/len(numeric),2) if numeric else None
    return render_template('grades.html',current=ongoing,archive=archive,period=current_period,
                           average=average,grades=rows)

@app.route('/requests',methods=['GET','POST'])
@student_required
def requests_page():
    if request.method=='POST':
        typ=request.form.get('type',''); comment=request.form.get('comment','').strip()
        if typ not in REQUEST_TYPES or len(comment)>1000 or (typ not in DOC_TYPES and len(comment)<5):
            flash('Выберите тип обращения и опишите вопрос (не менее 5 символов для заявлений).','error')
        else:
            rid=db().execute('''INSERT INTO requests(user_id,type,comment,status,created_at)
              VALUES(?,?,?,?,?)''',(session['uid'],typ,comment,'Принята',NOW())).lastrowid
            record_history(rid,'Принята','Заявка зарегистрирована')
            db().commit()
            flash(f'Заявка №{rid} отправлена. Следите за этапами обработки.','success')
            return redirect(url_for('request_detail',rid=rid))
    rows=db().execute('''SELECT r.*, d.id AS document_id FROM requests r
        LEFT JOIN request_documents d ON d.request_id=r.id WHERE r.user_id=? ORDER BY r.id DESC''',
        (session['uid'],)).fetchall()
    return render_template('requests.html',requests=rows)


@app.route('/requests/<int:rid>')
@login_required
def request_detail(rid):
    row=get_request_or_404(rid)
    history=db().execute('''SELECT h.*,u.full_name AS actor FROM request_history h LEFT JOIN users u ON u.id=h.actor_id
       WHERE h.request_id=? ORDER BY h.id''',(rid,)).fetchall()
    msgs=db().execute('''SELECT m.*,u.full_name,u.role FROM request_messages m JOIN users u ON u.id=m.author_id
       WHERE m.request_id=? ORDER BY m.id''',(rid,)).fetchall()
    doc=db().execute('SELECT id,filename,created_at FROM request_documents WHERE request_id=?',(rid,)).fetchone()
    return render_template('request_detail.html',item=row,history=history,messages=msgs,document=doc)


@app.route('/requests/<int:rid>/cancel',methods=['POST'])
@student_required
def cancel_request(rid):
    row=get_request_or_404(rid)
    if row['status']!='Принята':
        flash('Отменить можно только ещё не принятую в работу заявку.','error')
    else:
        db().execute("UPDATE requests SET status='Отменена',resolved_at=? WHERE id=?",(NOW(),rid))
        record_history(rid,'Отменена','Студент отменил заявку')
        audit('request_cancelled',f'request:{rid}')
        db().commit(); flash('Заявка отменена.','success')
    return redirect(url_for('request_detail',rid=rid))


@app.route('/requests/<int:rid>/reply',methods=['POST'])
@student_required
def student_reply(rid):
    row=get_request_or_404(rid)
    if row['status']!='Требует уточнения':
        abort(400)
    reply=request.form.get('reply','').strip()
    if not 2<=len(reply)<=1000:
        flash('Уточнение должно содержать от 2 до 1000 символов.','error')
        return redirect(url_for('request_detail',rid=rid))
    db().execute('INSERT INTO request_messages(request_id,author_id,body,created_at) VALUES(?,?,?,?)',
                 (rid,session['uid'],reply,NOW()))
    db().execute("UPDATE requests SET status='В работе' WHERE id=?",(rid,))
    record_history(rid,'В работе','Студент предоставил уточнение')
    audit('request_replied',f'request:{rid}')
    db().commit()
    flash('Ответ получен. Заявка вернулась в работу.','success')
    return redirect(url_for('request_detail',rid=rid))


def get_pdf_font():
    """System-installed Cyrillic font; no font files shipped with the project."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    if 'CampusDejaVu' in pdfmetrics.getRegisteredFontNames():
        return 'CampusDejaVu'
    candidates=[os.environ.get('PULS_PDF_FONT',''),
      '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
      '/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf',
      '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
      'C:/Windows/Fonts/arial.ttf', 'C:/Windows/Fonts/tahoma.ttf']
    for filename in candidates:
        if filename and os.path.isfile(filename):
            pdfmetrics.registerFont(TTFont('CampusDejaVu',filename))
            return 'CampusDejaVu'
    raise RuntimeError('Нужен системный кириллический TTF-шрифт: установите fonts-dejavu-core или задайте PULS_PDF_FONT')


def build_demo_certificate(row):
    """Render a deliberately non-official Russian-language certificate, in-memory."""
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.utils import simpleSplit
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, HRFlowable
    from reportlab.lib.pagesizes import A4
    font=get_pdf_font()
    buffer=io.BytesIO()
    report=SimpleDocTemplate(buffer,pagesize=A4,rightMargin=58,leftMargin=58,topMargin=75,bottomMargin=78,
                             title='ПУЛЬС — демонстрационная справка')
    base=ParagraphStyle('Base',fontName=font,fontSize=11,leading=18,textColor=colors.HexColor('#172b38'))
    heading=ParagraphStyle('Heading',parent=base,fontSize=20,leading=29,alignment=TA_CENTER,spaceAfter=22)
    label=ParagraphStyle('Label',parent=base,fontSize=9,leading=14,textColor=colors.HexColor('#69797f'))
    safe=lambda value:html_escape(str(value),quote=False)
    story=[Paragraph('ПУЛЬС · CAMPUS OS',ParagraphStyle('Brand',parent=base,alignment=TA_CENTER,fontSize=12)),
           Spacer(1,13),Paragraph('ДЕМОНСТРАЦИОННЫЙ ДОКУМЕНТ',ParagraphStyle('Demo',parent=label,alignment=TA_CENTER,textColor=colors.HexColor('#bf6f20'))),
           Spacer(1,28),Paragraph(safe(row['type']),heading),
           Paragraph(f'Запрос № {row["id"]} · {date.today().strftime("%d.%m.%Y")}',label),
           Spacer(1,21),HRFlowable(width='100%',thickness=.7,color=colors.HexColor('#d6dbd7')),Spacer(1,22),
           Paragraph('Настоящим подтверждается, что',base),Spacer(1,12),
           Paragraph(f'<b>{safe(row["full_name"])}</b>',base),Spacer(1,15),
           Paragraph(f'обучается в демонстрационной модели университета по направлению «{safe(row["program"])}», '
                     f'{safe(row["faculty"])}, {row["course"]} курс, группа {safe(row["group_name"])}.',base),
           Spacer(1,20),Paragraph('Основание: заявка студента, обработанная в учебном офисе ПУЛЬС.',base),
           Spacer(1,33),Paragraph('Документ сгенерирован автоматически в конкурсном прототипе. '
                                 'Не является действительной справкой, не имеет юридической силы '
                                 'и не содержит электронной подписи.',
                                 ParagraphStyle('Warning',parent=base,fontSize=9,leading=15,textColor=colors.HexColor('#a65b27')))]
    def footer(canvas,doc):
        canvas.saveState()
        canvas.setFont(font,8)
        canvas.setFillColor(colors.HexColor('#a65b27'))
        canvas.drawString(58,44,'ДЕМО · Неофициальный конкурсный концепт')
        canvas.setFillColor(colors.HexColor('#69797f'))
        canvas.drawRightString(A4[0]-58,44,f'Страница {doc.page}')
        canvas.restoreState()
    report.build(story,onFirstPage=footer,onLaterPages=footer)
    return buffer.getvalue()


@app.route('/requests/<int:rid>/document')
@login_required
def download_document(rid):
    row=get_request_or_404(rid)
    if row['status']!='Готова':
        abort(404)
    doc=db().execute('SELECT * FROM request_documents WHERE request_id=?',(rid,)).fetchone()
    if not doc:
        abort(404)
    return send_file(io.BytesIO(doc['content']),mimetype='application/pdf',
                     as_attachment=True,download_name=doc['filename'],max_age=0)


@app.route('/notices')
@student_required
def notices():
    rows=db().execute('''SELECT n.*, nr.notice_id IS NOT NULL AS is_read FROM notices n
      LEFT JOIN notice_reads nr ON nr.notice_id=n.id AND nr.user_id=?
      WHERE n.user_id=? ORDER BY n.id DESC''',(session['uid'],session['uid'])).fetchall()
    return render_template('notices.html',notices=rows)


@app.route('/notices/<int:nid>/read',methods=['POST'])
@student_required
def mark_read(nid):
    row=db().execute('SELECT request_id FROM notices WHERE id=? AND user_id=?',(nid,session['uid'])).fetchone()
    if not row:
        abort(404)
    db().execute('INSERT OR IGNORE INTO notice_reads(user_id,notice_id) VALUES(?,?)',(session['uid'],nid))
    db().commit()
    if row['request_id']:
        return redirect(url_for('request_detail',rid=row['request_id']))
    return redirect(url_for('notices'))


@app.route('/notices/read-all',methods=['POST'])
@student_required
def mark_all_read():
    db().execute('''INSERT OR IGNORE INTO notice_reads(user_id,notice_id)
       SELECT ?,id FROM notices WHERE user_id=?''',(session['uid'],session['uid']))
    db().commit()
    flash('Уведомления отмечены прочитанными.','success')
    return redirect(url_for('notices'))


@app.route('/profile')
@login_required
def profile():
    return render_template('profile.html')


@app.route('/profile/password',methods=['POST'])
@login_required
def change_password():
    user=current_user()
    old=request.form.get('old_password',''); new=request.form.get('new_password','')
    if not check_password_hash(user['password_hash'],old):
        flash('Текущий пароль неверен','error')
    elif DEMO_MODE and user['username'] in ('student1','student2','dean','admin'):
        flash('Для публичного демоаккаунта пароль закреплён. Создайте новый аккаунт через администратора, чтобы проверить смену пароля.','error')
    elif not 8<=len(new)<=128 or new in ('1234','password','12345678'):
        flash('Новый пароль: от 8 до 128 символов, не используйте простой пароль.','error')
    else:
        db().execute('UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?',
                     (generate_password_hash(new),user['id']))
        audit('password_changed',f'user:{user["id"]}')
        db().commit(); flash('Пароль изменён','success')
    return redirect(url_for('profile'))


@app.route('/staff')
@staff_required
def staff_dashboard():
    rows=db().execute("""SELECT r.*,u.full_name,u.group_name,d.id AS document_id
      FROM requests r JOIN users u ON u.id=r.user_id
      LEFT JOIN request_documents d ON d.request_id=r.id
      ORDER BY CASE r.status WHEN 'Требует уточнения' THEN 0 WHEN 'Принята' THEN 1
      WHEN 'В работе' THEN 2 ELSE 3 END,r.id DESC""").fetchall()
    return render_template('staff_requests.html',requests=rows,
        queue_count=sum(r['status'] not in TERMINAL for r in rows))

@app.route('/staff/schedule')
@staff_required
def staff_schedule():
    groups=db().execute('SELECT * FROM academic_groups ORDER BY name').fetchall()
    group=request.args.get('group','')
    if not any(g['name']==group for g in groups):
        group=groups[0]['name'] if groups else ''
    kind=request.args.get('week','odd')
    if kind not in ('odd','even'): kind='odd'
    selected=resolve_date(request.args.get('date'))
    # The chosen calendar date is independent from the recurring template editor parity.
    slots=db().execute('SELECT * FROM bell_slots ORDER BY number').fetchall()
    lessons=[]
    for r in (entries_for_editor(db(),group,kind) if group else []):
        lesson=dict(r)
        linked=shared_rows(r)
        lesson['shared_groups']=[peer['group_name'] for peer in linked if peer['week_type'] in ('both',kind)]
        lesson['teacher_list']=load_items(r['teachers_json'],r['teacher'])
        lesson['room_list']=load_items(r['rooms_json'],r['room'])
        lessons.append(lesson)
    indexed={(l['weekday'],l['slot_id']):l for l in lessons}
    days_list=[dict(number=i,name=DAYS[i],items=[dict(slot=slot,lesson=indexed.get((i,slot['id']))) for slot in slots]) for i in range(7)]
    exceptions=db().execute("""SELECT o.*,s.number AS slot_number,s.starts,s.ends
       FROM schedule_overrides o JOIN bell_slots s ON s.id=o.slot_id WHERE o.group_name=? AND o.date=?
       ORDER BY s.number""",(group,selected.isoformat())).fetchall()
    selected_events=week_events(db(),group,selected)[selected.weekday()]['lessons'] if group else []
    return render_template('staff_schedule.html',groups=groups,group=group,kind=kind,slots=slots,
        editor_days=days_list,selected=selected,exceptions=exceptions,selected_events=selected_events,lesson_types=LESSON_TYPES,
        week_title=week_title(week_start(selected)),real_week=week_kind(selected))

@app.route('/staff/academic')
@staff_required
def staff_academic():
    students=db().execute("SELECT id,full_name,group_name FROM users WHERE role='student' AND is_active=1 ORDER BY group_name,full_name").fetchall()
    return render_template('staff_academic.html',students=students)

@app.route('/staff/announcements')
@staff_required
def staff_announcements():
    groups=db().execute('SELECT * FROM academic_groups ORDER BY name').fetchall()
    students=db().execute("SELECT id,full_name,group_name FROM users WHERE role='student' AND is_active=1 ORDER BY full_name").fetchall()
    return render_template('staff_announcements.html',groups=groups,students=students)

@app.route('/staff/requests/<int:rid>/action',methods=['POST'])
@staff_required
def staff_request_action(rid):
    row=get_request_or_404(rid)
    action=request.form.get('action','')
    if action=='reopen':
        reason=request.form.get('note','').strip()
        if row['status'] not in ('Готова','Отклонена') or not 5<=len(reason)<=1000:
            abort(400)
        # Revoke the previously issued file: stale output must not remain downloadable.
        db().execute('DELETE FROM request_documents WHERE request_id=?',(rid,))
        db().execute("UPDATE requests SET status='В работе',result_text='',resolved_at='' WHERE id=?",(rid,))
        record_history(rid,'В работе','Пересмотр результата: '+reason)
        notice(row['user_id'],f'Заявка №{rid}: результат пересматривается',reason,rid=rid)
        audit('request_reopened',f'request:{rid}')
        db().commit()
        flash('Обращение возвращено в работу, прежний результат отозван.','success')
        return redirect(url_for('request_detail',rid=rid))
    if row['status'] in TERMINAL:
        abort(400)
    note=request.form.get('note','').strip()
    result=request.form.get('result_text','').strip()
    if len(note)>1000 or len(result)>2000:
        abort(400)
    new_status=None
    if action=='take':
        if row['status']!='Принята':
            abort(400)
        new_status='В работе'; note=note or 'Сотрудник начал обработку'
    elif action=='ask':
        if not 5<=len(note)<=1000 or row['status']=='Требует уточнения':
            flash('Напишите конкретный вопрос студенту (от 5 символов).','error')
            return redirect(url_for('request_detail',rid=rid))
        new_status='Требует уточнения'
        db().execute('INSERT INTO request_messages(request_id,author_id,body,created_at) VALUES(?,?,?,?)',
                     (rid,session['uid'],note,NOW()))
    elif action=='reject':
        if len(note)<5:
            flash('Для отказа необходимо указать причину.','error')
            return redirect(url_for('request_detail',rid=rid))
        new_status='Отклонена'
    elif action=='complete':
        if row['status']=='Требует уточнения':
            abort(400)
        upload=request.files.get('document')
        content=None
        filename=None
        if upload and upload.filename:
            if not upload.filename.lower().endswith('.pdf'):
                flash('Поддерживаются только PDF-файлы.','error')
                return redirect(url_for('request_detail',rid=rid))
            content=upload.read(2 * 1024 * 1024 + 1)
            if len(content)>2*1024*1024 or not content.startswith(b'%PDF-'):
                flash('Некорректный PDF или файл больше 2 МБ.','error')
                return redirect(url_for('request_detail',rid=rid))
            # A file extension and PDF magic number are not enough: reject corrupt uploads.
            try:
                from pypdf import PdfReader
                parsed=PdfReader(io.BytesIO(content), strict=False)
                if parsed.is_encrypted or not 1<=len(parsed.pages)<=20:
                    raise ValueError('PDF encrypted, empty or too long')
            except Exception as exc:
                app.logger.warning('Rejected invalid PDF attachment: %s',exc)
                flash('PDF повреждён, защищён паролем или превышает 20 страниц.','error')
                return redirect(url_for('request_detail',rid=rid))
            filename=f'puls_result_{rid}.pdf'
        elif row['type'] in DOC_TYPES:
            try:
                content=build_demo_certificate(row)
            except (RuntimeError, OSError) as exc:
                app.logger.exception('Cannot generate PDF for request %s',rid)
                flash(f'Не удалось создать PDF: {exc}','error')
                return redirect(url_for('request_detail',rid=rid))
            filename=f'puls_spravka_{rid}.pdf'
        elif len(result)<10:
            flash('Для завершения обращения нужен содержательный ответ (от 10 символов) или PDF.','error')
            return redirect(url_for('request_detail',rid=rid))
        if content:
            db().execute('''INSERT INTO request_documents(request_id,filename,content,created_at)
                VALUES(?,?,?,?)''',(rid,filename,content,NOW()))
        new_status='Готова'
        db().execute('UPDATE requests SET result_text=? WHERE id=?',(result,rid))
        note=note or ('PDF подготовлен и доступен студенту' if content else 'Ответ направлен студенту')
    else:
        abort(400)
    db().execute('UPDATE requests SET status=?,resolved_at=? WHERE id=?',
                 (new_status,NOW() if new_status in TERMINAL else '',rid))
    record_history(rid,new_status,note)
    message={
        'В работе':'Ваше обращение взято в работу.',
        'Требует уточнения':f'Нужно уточнение: {note}',
        'Отклонена':f'Обращение отклонено. Причина: {note}',
        'Готова':'Результат готов: откройте заявку, чтобы получить документ или прочитать ответ.'
    }[new_status]
    notice(row['user_id'],f'Заявка №{rid}: {new_status}',message,rid=rid)
    audit('request_'+action,f'request:{rid}')
    db().commit()
    flash('Действие сохранено. Студент увидит новый этап и результат.','success')
    return redirect(url_for('request_detail',rid=rid))


@app.route('/staff/notice',methods=['POST'])
@staff_required
def staff_notice():
    uid=request.form.get('user_id',''); title=request.form.get('title','').strip(); body=request.form.get('body','').strip()
    if not 3<=len(title)<=100 or not 5<=len(body)<=500:
        abort(400)
    if uid=='all':
        targets=db().execute("SELECT id FROM users WHERE role='student' AND is_active=1").fetchall()
    elif uid.startswith('group:'):
        group=uid[6:]
        targets=db().execute("SELECT id FROM users WHERE role='student' AND is_active=1 AND group_name=?",(group,)).fetchall()
    elif uid.isdigit():
        targets=db().execute("SELECT id FROM users WHERE id=? AND role='student' AND is_active=1",(int(uid),)).fetchall()
        if not targets:
            abort(400)
    else:
        abort(400)
    for target in targets:
        notice(target['id'],title,body,'news')
    audit('notice_published',f'audience:{uid};count:{len(targets)}')
    db().commit()
    flash(f'Объявление доставлено {len(targets)} студентам.','success')
    return redirect(url_for('staff_announcements'))


def valid_time(value):
    return bool(re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',value or ''))


def notify_group_change(group, title, body, target_date=''):
    users=db().execute("SELECT id FROM users WHERE role='student' AND is_active=1 AND group_name=?",(group,)).fetchall()
    for u in users:
        notice(u['id'],title,body,'schedule',target_date=target_date)
    return len(users)


def selected_lesson_groups(primary):
    '''Only dean may choose already registered academic groups; prevent empty groups.'''
    names = list(dict.fromkeys(request.form.getlist('group_names') or [primary]))
    if not names or primary not in names or len(names)>12:
        abort(400)
    valid = {r['name'] for r in db().execute('SELECT name FROM academic_groups')}
    if any(name not in valid for name in names):
        abort(400)
    return names


def shared_rows(row):
    if row['shared_id']:
        return db().execute('SELECT * FROM group_lessons WHERE shared_id=? ORDER BY group_name',
                            (row['shared_id'],)).fetchall()
    return [row]


def session_form_metadata(previous=None):
    defaults=dict(previous) if previous else {}
    if previous:
        defaults['teacher']='; '.join(load_items(previous['teachers_json'],previous['teacher']))
        defaults['room']='; '.join(load_items(previous['rooms_json'],previous['room']))
    try:
        return metadata_from_form(request.form,defaults)
    except ValueError as exc:
        flash(str(exc),'error')
        return None


# Compatibility route for existing form links; slot-based interface is the primary editor.
@app.route('/staff/lesson',methods=['POST'])
@staff_required
def staff_add_lesson():
    return staff_schedule_add()


@app.route('/staff/lesson/<int:lid>', methods=['POST'])
@staff_required
def staff_lesson(lid):
    row = db().execute('SELECT * FROM group_lessons WHERE id=?',(lid,)).fetchone()
    if not row: abort(404)
    members=shared_rows(row)
    old_groups={r['group_name'] for r in members}
    chosen=selected_lesson_groups(row['group_name']) if request.form.getlist('group_names') else sorted(old_groups)
    meta=session_form_metadata(row)
    if not meta:return redirect(staff_schedule_url(row['group_name'],row['week_type'] if row['week_type']!='both' else 'odd'))
    subject=request.form.get('subject',row['subject']).strip()
    kind=request.form.get('week_type',row['week_type'])
    try: day=int(request.form.get('weekday',str(row['weekday'])))
    except ValueError: day=-1
    slot=group_and_slot(row['group_name'],request.form.get('slot_id',str(row['slot_id'])))
    if kind not in ('both','even','odd') or day not in range(7) or not 2<=len(subject)<=100:
        abort(400)
    member_ids={r['group_name']:r['id'] for r in members}
    for group in chosen:
        if conflict(db(),group,day,slot['id'],kind,exclude=member_ids.get(group)):
            flash(f'У группы {group} эта пара уже занята. Совместное занятие не сохранено.','error')
            return redirect(staff_schedule_url(row['group_name'],kind if kind!='both' else 'odd'))
    # Keep the other parity when editing a shared both-week event on one week.
    if row['week_type']=='both' and kind in ('odd','even'):
        opposite='even' if kind=='odd' else 'odd'
        for member in members:
            if member['week_type'] != 'both':
                continue
            db().execute('''INSERT INTO group_lessons(group_name,weekday,starts,ends,subject,teacher,room,
               changed,slot_id,week_type,lesson_type,teachers_json,rooms_json,shared_id)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
               (member['group_name'],member['weekday'],member['starts'],member['ends'],member['subject'],
                member['teacher'],member['room'],member['changed'],member['slot_id'],opposite,
                member['lesson_type'],member['teachers_json'],member['rooms_json'],
                row['shared_id'] if len(members)>1 else None))
    shared_id=secrets.token_hex(12) if len(chosen)>1 else None
    for member in members:
        if member['group_name'] not in chosen:
            db().execute('DELETE FROM group_lessons WHERE id=?',(member['id'],))
        else:
            db().execute('''UPDATE group_lessons SET subject=?,teacher=?,room=?,changed=1,
               weekday=?,week_type=?,slot_id=?,starts=?,ends=?,lesson_type=?,teachers_json=?,
               rooms_json=?,shared_id=? WHERE id=?''',
               (subject,meta['teacher'],meta['room'],day,kind,slot['id'],slot['starts'],slot['ends'],
                meta['lesson_type'],meta['teachers_json'],meta['rooms_json'],shared_id,member['id']))
    for group in set(chosen)-old_groups:
        db().execute('''INSERT INTO group_lessons(group_name,weekday,starts,ends,subject,teacher,room,
            changed,slot_id,week_type,lesson_type,teachers_json,rooms_json,shared_id)
            VALUES(?,?,?,?,?,?,?,1,?,?,?,?,?,?)''',
            (group,day,slot['starts'],slot['ends'],subject,meta['teacher'],meta['room'],slot['id'],kind,
             meta['lesson_type'],meta['teachers_json'],meta['rooms_json'],shared_id))
    total=0
    for group in old_groups|set(chosen):
        total += notify_group_change(group,'Изменено занятие',
            f'{DAYS[day]} · пара №{slot["number"]}: {subject}; {", ".join(meta["rooms"])}')
    audit('lesson_changed',f'lesson:{lid};groups:{",".join(chosen)}')
    db().commit()
    flash(f'Занятие обновлено для {len(chosen)} групп. Уведомлений: {total}.','success')
    return redirect(staff_schedule_url(row['group_name'],kind if kind!='both' else 'odd'))


@app.route('/staff/lesson/<int:lid>/delete',methods=['POST'])
@staff_required
def staff_delete_lesson(lid):
    row=db().execute('SELECT * FROM group_lessons WHERE id=?',(lid,)).fetchone()
    if not row:abort(404)
    reason=request.form.get('reason','').strip()
    if not 5<=len(reason)<=200:abort(400)
    members=shared_rows(row)
    for member in members:
        db().execute('DELETE FROM group_lessons WHERE id=?',(member['id'],))
        notify_group_change(member['group_name'],'Занятие удалено из регулярного расписания',
            f'{DAYS[member["weekday"]]}: {member["subject"]}. Причина: {reason}')
    audit('lesson_deleted',f'lesson:{lid};groups:{len(members)};reason:{reason}')
    db().commit()
    flash(f'Удалено для {len(members)} групп.','success')
    return redirect(staff_schedule_url(row['group_name'],row['week_type'] if row['week_type']!='both' else 'odd'))


@app.route('/staff/grades',methods=['POST'])
@staff_required
def staff_grade():
    uid=request.form.get('user_id',''); subject=request.form.get('subject','').strip()
    kind=request.form.get('kind',''); grade=request.form.get('grade','')
    period=request.form.get('period','2026/27 · осень')
    if not uid.isdigit() or not 3<=len(subject)<=100 or kind not in ('Текущий контроль','Экзамен','Зачёт') or grade not in ('2','3','4','5','Зачёт','Незачёт') or period not in ('2026/27 · осень','2025/26 · весна'):
        abort(400)
    if (kind in ('Экзамен','Текущий контроль') and not grade.isdigit()) or (kind=='Зачёт' and grade not in ('Зачёт','Незачёт')):
        abort(400)
    user=db().execute("SELECT id FROM users WHERE id=? AND role='student' AND is_active=1",(int(uid),)).fetchone()
    if not user:abort(400)
    old=db().execute('SELECT id FROM grades WHERE user_id=? AND subject=? AND kind=? AND period=?',
                     (int(uid),subject,kind,period)).fetchone()
    if old:
        db().execute('UPDATE grades SET grade=?,date=? WHERE id=?',(grade,date.today().isoformat(),old['id']))
    else:
        db().execute('INSERT INTO grades(user_id,subject,grade,kind,date,period) VALUES(?,?,?,?,?,?)',
                     (int(uid),subject,grade,kind,date.today().isoformat(),period))
    notice(int(uid),'Обновлена успеваемость',f'{period} · {subject}: {grade} ({kind})','grades')
    audit('grade_updated',f'student:{uid};subject:{subject};period:{period}')
    db().commit()
    flash('Результат сохранён с указанием периода и типа контроля.','success')
    return redirect(url_for('staff_academic'))


@app.route('/admin')
@admin_required
def admin_dashboard():
    users=db().execute('SELECT id,username,full_name,faculty,group_name,role,is_active,must_change_password FROM users ORDER BY role,full_name').fetchall()
    groups=db().execute('SELECT * FROM academic_groups ORDER BY name').fetchall()
    logs=db().execute('SELECT a.*,u.username AS actor FROM audit_log a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.id DESC LIMIT 50').fetchall()
    admin_tab=request.args.get('tab','import')
    if admin_tab not in ('import','users','journal'):
        admin_tab='import'
    return render_template('admin.html',users=users,groups=groups,logs=logs,admin_tab=admin_tab)


@app.route('/admin/groups',methods=['POST'])
@admin_required
def admin_create_group():
    name=request.form.get('name','').strip(); faculty=request.form.get('faculty','').strip(); program=request.form.get('program','').strip()
    try: course=int(request.form.get('course','0'))
    except ValueError: course=0
    if not all((name,faculty,program)) or any(len(x)>100 for x in (name,faculty,program)) or course not in range(1,7):
        abort(400)
    if DEMO_MODE and db().execute('SELECT COUNT(*) FROM academic_groups').fetchone()[0]>=30:
        flash('Лимит демогрупп достигнут.','error')
        return redirect(url_for('admin_dashboard',tab='users'))
    try:
        db().execute('INSERT INTO academic_groups(name,faculty,program,course) VALUES(?,?,?,?)',(name,faculty,program,course))
        audit('group_created',name); db().commit(); flash('Группа создана. Учебный офис может добавить для неё занятия.','success')
    except sqlite3.IntegrityError:
        db().rollback(); flash('Такая группа уже существует','error')
    return redirect(url_for('admin_dashboard',tab='users'))


@app.route('/admin/groups/<int:gid>/update',methods=['POST'])
@admin_required
def admin_update_group(gid):
    group=db().execute('SELECT * FROM academic_groups WHERE id=?',(gid,)).fetchone()
    if not group:
        abort(404)
    faculty=request.form.get('faculty','').strip()
    program=request.form.get('program','').strip()
    try:
        course=int(request.form.get('course','0'))
    except ValueError:
        course=0
    if not faculty or not program or max(len(faculty),len(program))>100 or course not in range(1,7):
        abort(400)
    db().execute('UPDATE academic_groups SET faculty=?,program=?,course=? WHERE id=?',
                 (faculty,program,course,gid))
    db().execute('''UPDATE users SET faculty=?,program=?,course=?
      WHERE role='student' AND group_name=?''',(faculty,program,course,group['name']))
    students=db().execute("SELECT id FROM users WHERE role='student' AND is_active=1 AND group_name=?",
                          (group['name'],)).fetchall()
    for student in students:
        notice(student['id'],'Обновлены данные группы',
               f'Группа {group["name"]}: {faculty}, {program}, {course} курс','news')
    audit('group_updated',f'group:{group["name"]}')
    db().commit()
    flash('Группа обновлена; данные студентов синхронизированы.','success')
    return redirect(url_for('admin_dashboard',tab='users'))


@app.route('/admin/users',methods=['POST'])
@admin_required
def admin_create_user():
    username=request.form.get('username','').strip().lower(); full_name=request.form.get('full_name','').strip()
    role=request.form.get('role',''); group=request.form.get('group_name',''); password=request.form.get('password','')
    if not re.fullmatch(r'[a-z0-9_.-]{3,32}',username) or not 3<=len(full_name)<=100 or role not in ('staff','admin') or not 8<=len(password)<=128:
        abort(400)
    faculty,program,course,group_name='Учебный офис','Сотрудник',0,'Деканат'
    if DEMO_MODE and db().execute('SELECT COUNT(*) FROM users').fetchone()[0]>=80:
        flash('Лимит демоданных: новые аккаунты временно не создаются.','error')
        return redirect(url_for('admin_dashboard',tab='users'))
    try:
        db().execute('''INSERT INTO users(username,password_hash,full_name,faculty,program,course,group_name,role,must_change_password)
          VALUES(?,?,?,?,?,?,?,?,1)''',(username,generate_password_hash(password),full_name,faculty,program,course,group_name,role))
        audit('user_created',username);db().commit()
        flash('Аккаунт создан. Временный пароль передайте безопасным способом.','success')
    except sqlite3.IntegrityError:
        db().rollback();flash('Этот логин уже занят','error')
    return redirect(url_for('admin_dashboard',tab='users'))


@app.route('/admin/users/<int:uid>/update',methods=['POST'])
@admin_required
def admin_update_user(uid):
    user=db().execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
    if not user: abort(404)
    if DEMO_MODE and user['username'] in ('student1','student2','dean','admin'):
        flash('Публичные демоаккаунты защищены от изменения роли и блокировки.','error')
        return redirect(url_for('admin_dashboard',tab='users'))
    role=request.form.get('role',''); group=request.form.get('group_name',''); active=request.form.get('is_active')=='1'
    if role not in ('student','staff','admin'): abort(400)
    if uid==session['uid'] and (not active or role!='admin'): abort(400)
    if user['role']=='admin' and (not active or role!='admin'):
        count=db().execute("SELECT COUNT(*) FROM users WHERE role='admin' AND is_active=1").fetchone()[0]
        if count<=1: abort(400)
    if active and db().execute('SELECT 1 FROM account_activations WHERE user_id=?',(uid,)).fetchone():
        flash('Аккаунт из реестра должен быть активирован по персональной ссылке.','error')
        return redirect(url_for('admin_dashboard',tab='users'))
    if role=='student':
        gr=db().execute('SELECT * FROM academic_groups WHERE name=?',(group,)).fetchone()
        if not gr: abort(400)
        fields=(role,int(active),gr['faculty'],gr['program'],gr['course'],gr['name'],uid)
    else:
        fields=(role,int(active),'Учебный офис','Сотрудник',0,'Деканат',uid)
    db().execute('''UPDATE users SET role=?,is_active=?,faculty=?,program=?,course=?,group_name=? WHERE id=?''',fields)
    audit('user_updated',f'user:{uid};role:{role};active:{int(active)}')
    db().commit();flash('Данные пользователя обновлены','success')
    return redirect(url_for('admin_dashboard',tab='users'))


@app.route('/admin/users/<int:uid>/reset-password',methods=['POST'])
@admin_required
def admin_reset_password(uid):
    user=db().execute('SELECT username FROM users WHERE id=?',(uid,)).fetchone()
    if not user: abort(404)
    if DEMO_MODE and user['username'] in ('student1','student2','dean','admin'):
        flash('Пароль публичного демоаккаунта менять нельзя.','error')
        return redirect(url_for('admin_dashboard',tab='users'))
    password=request.form.get('password','')
    if not 8<=len(password)<=128: abort(400)
    db().execute('UPDATE users SET password_hash=?,must_change_password=1 WHERE id=?',
                 (generate_password_hash(password),uid))
    audit('password_reset',f'user:{uid}');db().commit();flash('Временный пароль установлен','success')
    return redirect(url_for('admin_dashboard',tab='users'))


# --- v5: calendar editor and controlled enrolment lifecycle --------------------

def staff_schedule_url(group,week='odd',selected=None):
    return url_for('staff_schedule',group=group,week=week,date=(selected or date.today()).isoformat())


def group_and_slot(group,slot_id):
    if not db().execute('SELECT 1 FROM academic_groups WHERE name=?',(group,)).fetchone():
        abort(400)
    try: sid=int(slot_id)
    except (TypeError,ValueError): abort(400)
    slot=db().execute('SELECT * FROM bell_slots WHERE id=?',(sid,)).fetchone()
    if not slot: abort(400)
    return slot


@app.route('/staff/slots',methods=['POST'])
@staff_required
def staff_slots():
    try: number=int(request.form.get('number','0'))
    except ValueError: number=0
    starts=request.form.get('starts','');ends=request.form.get('ends','')
    if not 1<=number<=12 or not valid_time(starts) or not valid_time(ends) or starts>=ends:
        abort(400)
    overlapping=db().execute('SELECT 1 FROM bell_slots WHERE number!=? AND starts<? AND ends>?',
                             (number,ends,starts)).fetchone()
    if overlapping:
        flash('Время пары пересекается с другой парой.','error')
    else:
        slot=db().execute('SELECT id,starts,ends FROM bell_slots WHERE number=?',(number,)).fetchone()
        if slot:
            if slot['starts']!=starts or slot['ends']!=ends:
                db().execute('UPDATE bell_slots SET starts=?,ends=? WHERE id=?',(starts,ends,slot['id']))
                db().execute('UPDATE group_lessons SET starts=?,ends=? WHERE slot_id=?',(starts,ends,slot['id']))
                groups=db().execute('SELECT DISTINCT group_name FROM group_lessons WHERE slot_id=?',(slot['id'],)).fetchall()
                for group in groups:
                    notify_group_change(group['group_name'],'Изменилось время пары',
                        f'Пара №{number}: теперь {starts}–{ends}. Проверьте календарь.')
        else:
            db().execute('INSERT INTO bell_slots(number,starts,ends) VALUES(?,?,?)',(number,starts,ends))
        audit('bell_slot_saved',f'pair:{number}:{starts}-{ends}')
        db().commit()
        flash(f'Время пары №{number} сохранено.','success')
    return redirect(url_for('staff_schedule',group=request.form.get('group',''))+'#bells')


@app.route('/staff/schedule/add',methods=['POST'])
@staff_required
def staff_schedule_add():
    group=request.form.get('group_name','')
    slot=group_and_slot(group,request.form.get('slot_id'))
    groups=selected_lesson_groups(group)
    try: weekday=int(request.form.get('weekday','-1'))
    except ValueError: weekday=-1
    kind=request.form.get('week_type','')
    subject=request.form.get('subject','').strip()
    meta=session_form_metadata()
    if not meta:return redirect(staff_schedule_url(group,kind if kind in ('odd','even') else 'odd'))
    if weekday not in range(7) or kind not in ('both','even','odd') or not 2<=len(subject)<=100:
        abort(400)
    for name in groups:
        if conflict(db(),name,weekday,slot['id'],kind):
            flash(f'У группы {name} уже есть пара в это время. Изменение не сохранено.','error')
            return redirect(staff_schedule_url(group,kind if kind!='both' else 'odd'))
    token=secrets.token_hex(12) if len(groups)>1 else None
    for name in groups:
        db().execute('''INSERT INTO group_lessons(group_name,weekday,starts,ends,subject,teacher,room,
            changed,slot_id,week_type,lesson_type,teachers_json,rooms_json,shared_id)
            VALUES(?,?,?,?,?,?,?,0,?,?,?,?,?,?)''',
            (name,weekday,slot['starts'],slot['ends'],subject,meta['teacher'],meta['room'],slot['id'],
             kind,meta['lesson_type'],meta['teachers_json'],meta['rooms_json'],token))
        notify_group_change(name,'Опубликовано расписание',
            f'{DAYS[weekday]}, пара №{slot["number"]}, {subject}; {", ".join(meta["rooms"])}.')
    audit('lesson_created',f'groups:{",".join(groups)};kind:{kind};subject:{subject}')
    db().commit()
    flash(f'Пара опубликована для {len(groups)} групп.','success')
    return redirect(staff_schedule_url(group,kind if kind!='both' else 'odd'))


@app.route('/staff/schedule/copy',methods=['POST'])
@staff_required
def staff_schedule_copy():
    group=request.form.get('group_name','')
    source=request.form.get('source','')
    if source not in ('odd','even') or request.form.get('confirm')!='yes': abort(400)
    if not db().execute('SELECT 1 FROM academic_groups WHERE name=?',(group,)).fetchone():abort(400)
    target='odd' if source=='even' else 'even'
    # Explicit destructive operation: target-only timetable is replaced by source timetable.
    db().execute('DELETE FROM group_lessons WHERE group_name=? AND week_type=?',(group,target))
    db().execute("UPDATE group_lessons SET week_type='both' WHERE group_name=? AND week_type=?",(group,source))
    count=notify_group_change(group,'Расписание синхронизировано',
        f'Чётная и нечётная недели теперь совпадают по шаблону {"чётной" if source=="even" else "нечётной"} недели. Точечные переносы сохраняются.')
    audit('schedule_synchronized',f'group:{group};source:{source}')
    db().commit()
    flash(f'Обе недели совпадают. Обновление отправлено {count} студентам.','success')
    return redirect(staff_schedule_url(group,source))


@app.route('/staff/schedule/exception',methods=['POST'])
@staff_required
def staff_schedule_exception():
    group=request.form.get('group_name','')
    slot=group_and_slot(group,request.form.get('slot_id'))
    groups=selected_lesson_groups(group)
    try: selected=date.fromisoformat(request.form.get('date',''))
    except ValueError:abort(400)
    if abs((selected-date.today()).days)>730:abort(400)
    action=request.form.get('action','')
    if action not in ('replace','cancel','extra'):abort(400)
    reason=request.form.get('reason','').strip()
    subject=request.form.get('subject','').strip()
    if not 5<=len(reason)<=200:abort(400)
    meta=session_form_metadata() if action!='cancel' else None
    if action!='cancel' and not meta:
        return redirect(staff_schedule_url(group,week_kind(selected),selected)+'#exceptions')
    if action!='cancel' and not 2<=len(subject)<=100:abort(400)
    for name in groups:
        existing=db().execute('''SELECT id FROM group_lessons WHERE group_name=? AND weekday=?
            AND slot_id=? AND week_type IN ('both',?) LIMIT 1''',
            (name,selected.weekday(),slot['id'],week_kind(selected))).fetchone()
        if action in ('cancel','replace') and not existing:
            flash(f'У группы {name} нет регулярной пары для замены/отмены.','error')
            return redirect(staff_schedule_url(group,week_kind(selected),selected)+'#exceptions')
        if action=='extra' and existing:
            flash(f'У группы {name} пара уже есть. Выбери замену или отмену.','error')
            return redirect(staff_schedule_url(group,week_kind(selected),selected)+'#exceptions')
    shared_id=secrets.token_hex(12) if len(groups)>1 else None
    for name in groups:
        db().execute('''INSERT INTO schedule_overrides(group_name,date,slot_id,action,subject,teacher,room,reason,
          lesson_type,teachers_json,rooms_json,shared_id)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(group_name,date,slot_id) DO UPDATE SET
          action=excluded.action,subject=excluded.subject,teacher=excluded.teacher,
          room=excluded.room,reason=excluded.reason,lesson_type=excluded.lesson_type,
          teachers_json=excluded.teachers_json,rooms_json=excluded.rooms_json,shared_id=excluded.shared_id''',
          (name,selected.isoformat(),slot['id'],action,subject if meta else '',
           meta['teacher'] if meta else '',meta['room'] if meta else '',reason,
           meta['lesson_type'] if meta else 'ПР',meta['teachers_json'] if meta else '[]',
           meta['rooms_json'] if meta else '[]',shared_id))
        title='Занятие отменено' if action=='cancel' else 'Изменение занятия' if action=='replace' else 'Дополнительное занятие'
        notify_group_change(name,title,f'{selected.strftime("%d.%m.%Y")}, пара №{slot["number"]}. {reason}',
                            target_date=selected.isoformat())
    audit('schedule_exception',f'groups:{",".join(groups)};date:{selected};slot:{slot["number"]};action:{action}')
    db().commit()
    flash(f'Изменение сохранено только на {selected.strftime("%d.%m.%Y")} для {len(groups)} групп.','success')
    return redirect(staff_schedule_url(group,week_kind(selected),selected)+'#exceptions')


@app.route('/staff/schedule/exception/<int:eid>/delete',methods=['POST'])
@staff_required
def staff_schedule_exception_delete(eid):
    o=db().execute('SELECT * FROM schedule_overrides WHERE id=?',(eid,)).fetchone()
    if not o:abort(404)
    reason=request.form.get('reason','').strip()
    if not 5<=len(reason)<=200:abort(400)
    members=db().execute('SELECT * FROM schedule_overrides WHERE shared_id=?',(o['shared_id'],)).fetchall() if o['shared_id'] else [o]
    for member in members:
        db().execute('DELETE FROM schedule_overrides WHERE id=?',(member['id'],))
        notify_group_change(member['group_name'],'Отменено точечное изменение',
            f'{member["date"]}: {reason}. Действует обычное расписание.',target_date=member['date'])
    audit('schedule_exception_removed',f'exception:{eid};groups:{len(members)};reason:{reason}')
    db().commit();flash('Точечное изменение отменено.','success')
    return redirect(staff_schedule_url(o['group_name'],week_kind(date.fromisoformat(o['date'])),date.fromisoformat(o['date']))+'#exceptions')


@app.route('/admin/import',methods=['POST'])
@admin_required
def admin_import_preview():
    upload=request.files.get('registry')
    if not upload or not upload.filename.lower().endswith('.csv'):
        flash('Выберите CSV-файл (не Excel XLSX) с вымышленными данными.','error')
        return redirect(url_for('admin_dashboard',tab='import')+'#upload')
    try:rows=parse_registry(upload.read(500_001))
    except ValueError as exc:
        flash(str(exc),'error');return redirect(url_for('admin_dashboard',tab='import')+'#upload')
    groups={g['name'] for g in db().execute('SELECT name FROM academic_groups')}
    known={u['username'] for u in db().execute('SELECT username FROM users')}
    for r in rows:
        if r['group_name'] not in groups:r['errors'].append('Группа не найдена — сначала создайте её')
        if r['username'] in known:r['errors'].append('Логин уже используется')
    # Preview is stored server-side, never in a client-side cookie.
    batch=db().execute('INSERT INTO import_batches(actor_id,created_at,payload) VALUES(?,?,?)',
                       (session['uid'],NOW(),json.dumps(rows,ensure_ascii=False))).lastrowid
    db().commit()
    return redirect(url_for('admin_import_review',batch_id=batch))


@app.route('/admin/import/<int:batch_id>')
@admin_required
def admin_import_review(batch_id):
    batch=db().execute('SELECT * FROM import_batches WHERE id=? AND actor_id=?',(batch_id,session['uid'])).fetchone()
    if not batch:abort(404)
    rows=json.loads(batch['payload'])
    return render_template('import_review.html',batch=batch,rows=rows,
                           errors=sum(bool(r['errors']) for r in rows))


@app.route('/admin/import/<int:batch_id>/confirm',methods=['POST'])
@admin_required
def admin_import_confirm(batch_id):
    batch=db().execute('SELECT * FROM import_batches WHERE id=? AND actor_id=? AND status=?',
                       (batch_id,session['uid'],'preview')).fetchone()
    if not batch:abort(404)
    rows=json.loads(batch['payload'])
    if not rows or any(r['errors'] for r in rows):
        flash('Сначала исправьте ошибки реестра и загрузите файл снова.','error')
        return redirect(url_for('admin_import_review',batch_id=batch_id))
    if DEMO_MODE and db().execute('SELECT COUNT(*) FROM users').fetchone()[0]+len(rows)>80:
        flash('Превышен лимит 80 демоаккаунтов.','error')
        return redirect(url_for('admin_import_review',batch_id=batch_id))
    invitations=[]
    # Serialize validation and inserts in the same transaction; avoid partially created cohorts.
    try:
        conn=db()
        conn.execute('BEGIN IMMEDIATE')
        for r in rows:
            group=conn.execute('SELECT * FROM academic_groups WHERE name=?',(r['group_name'],)).fetchone()
            if not group:raise ValueError('Группа была удалена. Загрузите CSV повторно.')
            token=secrets.token_urlsafe(24)
            uid=conn.execute('''INSERT INTO users(username,password_hash,full_name,faculty,program,course,group_name,
                role,is_active,must_change_password) VALUES(?,?,?,?,?,?,?,'student',0,1)''',
                (r['username'],generate_password_hash(secrets.token_urlsafe(32)),r['full_name'],
                 group['faculty'],group['program'],group['course'],group['name'])).lastrowid
            conn.execute('INSERT INTO account_activations(user_id,token_hash,expires) VALUES(?,?,?)',
                         (uid,hashlib.sha256(token.encode()).hexdigest(),
                          (datetime.now()+timedelta(days=7)).isoformat(timespec='seconds')))
            invitations.append((r['username'],r['full_name'],r['group_name'],url_for('activate',token=token,_external=True)))
        conn.execute('UPDATE import_batches SET status=? WHERE id=?',('approved',batch_id))
        audit('registry_approved',f'batch:{batch_id};students:{len(rows)}')
        conn.commit()
    except (sqlite3.IntegrityError,ValueError) as exc:
        db().rollback()
        flash('Реестр изменился или содержит дубликаты: '+str(exc),'error')
        return redirect(url_for('admin_import_review',batch_id=batch_id))
    # Never write invitation tokens to a public upload folder or logs; one-time CSV delivery.
    stream=io.StringIO(newline='')
    writer=csv.writer(stream)
    writer.writerow(('username','full_name','group_name','activation_url'))
    writer.writerows(invitations)
    flash('Аккаунты созданы. Ссылки активации выдаются однократно в CSV.','success')
    buffer=io.BytesIO(('\ufeff'+stream.getvalue()).encode('utf-8'))
    return send_file(buffer,mimetype='text/csv; charset=utf-8',as_attachment=True,
                     download_name=f'puls_invitations_{batch_id}.csv',max_age=0)


@app.route('/activate/<token>',methods=['GET','POST'])
def activate(token):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{20,70}',token):abort(404)
    digest=hashlib.sha256(token.encode()).hexdigest()
    row=db().execute('''SELECT a.*,u.full_name,u.username FROM account_activations a JOIN users u ON u.id=a.user_id
       WHERE a.token_hash=?''',(digest,)).fetchone()
    if not row or datetime.fromisoformat(row['expires'])<datetime.now():abort(404)
    if request.method=='POST':
        password=request.form.get('password','')
        if not 8<=len(password)<=128 or password.lower() in ('password','12345678','qwerty123','123456789'):
            flash('Пароль должен содержать от 8 до 128 символов, не используйте очевидные комбинации.','error')
        else:
            db().execute('UPDATE users SET password_hash=?,is_active=1,must_change_password=0 WHERE id=?',
                         (generate_password_hash(password),row['user_id']))
            db().execute('DELETE FROM account_activations WHERE user_id=?',(row['user_id'],))
            db().commit()
            flash('Аккаунт активирован. Войдите с выбранным паролем.','success')
            return redirect(url_for('login'))
    return render_template('activate.html',student=row)



@app.route('/healthz')
def healthz():
    try:
        db().execute('SELECT 1 FROM users LIMIT 1').fetchone()
        return 'ok',200
    except sqlite3.Error:
        return 'unavailable',503


@app.errorhandler(403)
def forbidden(error):
    return render_template('error.html',message='Нет доступа к этому разделу.'),403


@app.errorhandler(400)
def invalid_request(error):
    return render_template('error.html',message='Некорректный запрос. Обновите страницу и повторите действие.'),400


@app.errorhandler(404)
def not_found(error):
    return render_template('error.html',message='Страница не найдена или недоступна для этого аккаунта.'),404


@app.errorhandler(413)
def too_large(error):
    return render_template('error.html',message='Файл слишком большой. Максимальный размер запроса — 3 МБ.'),413


if __name__=='__main__':
    with app.app_context():
        init_db()
    app.run(host='127.0.0.1',port=int(os.environ.get('PORT','5000')),debug=False)
