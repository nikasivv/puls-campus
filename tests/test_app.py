"""Integration checks. Run: python -m unittest discover -s tests -v"""
import io
import os
import tempfile
import unittest

import app as application


class CampusWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old_db=application.DB
        self.old_demo=application.DEMO_MODE
        application.DB=os.path.join(self.tmp.name,'campus_test.db')
        application.DEMO_MODE=True
        application.app.config.update(TESTING=True)
        with application.app.app_context():
            application.init_db()
        self.client=application.app.test_client()

    def tearDown(self):
        application.DB=self.old_db
        application.DEMO_MODE=self.old_demo
        self.tmp.cleanup()

    def csrf(self):
        with self.client.session_transaction() as state:
            return state['_csrf']

    def login(self,username='student1',password='1234'):
        self.client.get('/login')
        return self.client.post('/login',data={'_csrf':self.csrf(),'username':username,'password':password},follow_redirects=True)

    def demo(self,who):
        self.client.get('/login')
        return self.client.post('/demo-login',data={'_csrf':self.csrf(),'account':who},follow_redirects=True)

    def logout(self):
        return self.client.post('/logout',data={'_csrf':self.csrf()})

    def post(self,path,**data):
        return self.client.post(path,data={'_csrf':self.csrf(),**data},follow_redirects=True)

    def select(self,sql,args=()):
        with application.app.app_context():
            return application.db().execute(sql,args).fetchall()

    def create_request(self,typ='Справка об обучении',comment='Для предъявления по месту требования'):
        self.post('/requests',type=typ,comment=comment)
        return self.select('SELECT max(id) AS id FROM requests')[0]['id']

    def test_public_and_demo_logins(self):
        self.assertEqual(self.client.get('/grades').status_code,302)
        for username,expected in [('student1',b'\xd0\x90\xd0\xbb\xd0\xb5\xd0\xba\xd1\x81'),('student2',b'\xd0\x9c\xd0\xb0\xd1\x80\xd0\xba')]:
            response=self.demo(username)
            self.assertEqual(response.status_code,200)
            self.assertIn(expected,self.client.get('/profile').data)
            self.logout()
        self.assertEqual(self.demo('dean').status_code,200)
        self.assertEqual(self.client.get('/staff').status_code,200)
        self.logout()
        self.assertEqual(self.demo('admin').status_code,200)
        self.assertEqual(self.client.get('/admin').status_code,200)

    def test_demo_mode_can_be_disabled(self):
        application.DEMO_MODE=False
        self.client.get('/login')
        self.assertEqual(self.client.post('/demo-login',data={'_csrf':self.csrf(),'account':'admin'}).status_code,404)
        self.assertNotIn(b'/demo-login',self.client.get('/login').data)

    def test_student_pages_and_group_schedule(self):
        self.login()
        for url in ['/','/schedule','/schedule?day=2','/grades','/requests','/notices','/profile']:
            self.assertEqual(self.client.get(url).status_code,200,url)
        self.assertIn('Цифровая экономика'.encode(),self.client.get('/schedule?day=0').data)

    def test_student_isolation_and_staff_roles(self):
        self.login('student1')
        rid=self.create_request(comment='Только Александра должна это видеть')
        self.assertEqual(self.client.get('/staff').status_code,403)
        self.assertEqual(self.client.get('/admin').status_code,403)
        self.logout();self.login('student2')
        self.assertEqual(self.client.get(f'/requests/{rid}').status_code,404)
        self.assertEqual(self.client.get(f'/requests/{rid}/document').status_code,404)
        self.assertEqual(self.post(f'/requests/{rid}/cancel').status_code,404)
        self.assertNotIn('Только Александра'.encode(),self.client.get('/requests').data)
        self.logout();self.login('admin')
        self.assertEqual(self.client.get(f'/requests/{rid}').status_code,403)
        self.assertEqual(self.client.get('/grades').status_code,403)

    def test_complete_certificate_creates_real_pdf_and_notification(self):
        self.login('student1')
        rid=self.create_request()
        self.logout();self.login('dean')
        self.post(f'/staff/requests/{rid}/action',action='take')
        self.assertIn('В работе'.encode(),self.client.get(f'/requests/{rid}').data)
        self.post(f'/staff/requests/{rid}/action',action='complete',result_text='Справка выдана')
        self.assertEqual(self.select('SELECT status FROM requests WHERE id=?',(rid,))[0]['status'],'Готова')
        self.logout();self.login('student1')
        document=self.client.get(f'/requests/{rid}/document')
        self.assertEqual(document.status_code,200)
        self.assertTrue(document.data.startswith(b'%PDF-'))
        self.assertIn(b'application/pdf',document.headers['Content-Type'].encode())
        self.assertIn('Скачать результат'.encode(),self.client.get(f'/requests/{rid}').data)
        self.assertIn('Заявка №'.encode(),self.client.get('/notices').data)

    def test_other_question_needs_real_result(self):
        self.login('student1')
        rid=self.create_request('Другой вопрос','Прошу проверить ошибку в расписании')
        self.logout();self.login('dean')
        self.post(f'/staff/requests/{rid}/action',action='take')
        result=self.post(f'/staff/requests/{rid}/action',action='complete',result_text='ок')
        self.assertIn('Для завершения'.encode(),result.data)
        self.assertEqual(self.select('SELECT status FROM requests WHERE id=?',(rid,))[0]['status'],'В работе')
        self.post(f'/staff/requests/{rid}/action',action='complete',result_text='Проверили расписание. Аудитория исправлена.')
        self.assertEqual(self.select('SELECT status FROM requests WHERE id=?',(rid,))[0]['status'],'Готова')
        self.logout();self.login('student1')
        self.assertIn('Аудитория исправлена'.encode(),self.client.get(f'/requests/{rid}').data)
        self.assertEqual(self.client.get(f'/requests/{rid}/document').status_code,404)

    def test_clarification_full_cycle(self):
        self.login('student1')
        rid=self.create_request()
        self.logout();self.login('dean')
        self.post(f'/staff/requests/{rid}/action',action='ask',note='Для какой организации нужна справка?')
        self.assertEqual(self.select('SELECT status FROM requests WHERE id=?',(rid,))[0]['status'],'Требует уточнения')
        self.assertEqual(self.post(f'/staff/requests/{rid}/action',action='complete').status_code,400)
        self.logout();self.login('student1')
        self.assertIn('Твой'.encode(),self.client.get('/').data)
        self.post(f'/requests/{rid}/reply',reply='Для предоставления по месту требования')
        self.assertEqual(self.select('SELECT status FROM requests WHERE id=?',(rid,))[0]['status'],'В работе')
        self.assertEqual(len(self.select('SELECT id FROM request_messages WHERE request_id=?',(rid,))),2)
        self.assertEqual(self.post(f'/requests/{rid}/reply',reply='Повтор').status_code,400)
        self.logout();self.login('dean')
        self.post(f'/staff/requests/{rid}/action',action='complete')
        self.assertEqual(self.select('SELECT status FROM requests WHERE id=?',(rid,))[0]['status'],'Готова')

    def test_cancel_and_reject_require_rules(self):
        self.login('student1')
        rid=self.create_request()
        self.post(f'/requests/{rid}/cancel')
        self.assertEqual(self.select('SELECT status FROM requests WHERE id=?',(rid,))[0]['status'],'Отменена')
        self.logout();self.login('dean')
        self.assertEqual(self.post(f'/staff/requests/{rid}/action',action='complete').status_code,400)
        self.logout();self.login('student1')
        rid2=self.create_request()
        self.logout();self.login('dean')
        self.post(f'/staff/requests/{rid2}/action',action='reject',note='')
        self.assertEqual(self.select('SELECT status FROM requests WHERE id=?',(rid2,))[0]['status'],'Принята')
        self.post(f'/staff/requests/{rid2}/action',action='reject',note='Отсутствуют необходимые основания')
        self.assertEqual(self.select('SELECT status FROM requests WHERE id=?',(rid2,))[0]['status'],'Отклонена')

    def test_pdf_upload_accepts_only_limited_pdf(self):
        self.login('student1')
        rid=self.create_request('Другой вопрос','Просьба получить заверенный ответ')
        self.logout();self.login('dean')
        self.post(f'/staff/requests/{rid}/action',action='take')
        response=self.client.post(f'/staff/requests/{rid}/action',data={
            '_csrf':self.csrf(),'action':'complete','document':(io.BytesIO(b'plain text'),'answer.pdf')},
            content_type='multipart/form-data',follow_redirects=True)
        self.assertIn('Некорректный PDF'.encode(),response.data)
        self.assertEqual(self.select('SELECT status FROM requests WHERE id=?',(rid,))[0]['status'],'В работе')
        from reportlab.pdfgen import canvas
        valid=io.BytesIO()
        document_canvas=canvas.Canvas(valid)
        document_canvas.drawString(70,770,'Demo result')
        document_canvas.save()
        response=self.client.post(f'/staff/requests/{rid}/action',data={
            '_csrf':self.csrf(),'action':'complete','document':(io.BytesIO(valid.getvalue()),'answer.pdf')},
            content_type='multipart/form-data',follow_redirects=True)
        self.assertEqual(self.select('SELECT status FROM requests WHERE id=?',(rid,))[0]['status'],'Готова')

    def test_notice_and_grades_workflow(self):
        self.login('dean')
        self.post('/staff/notice',user_id='group:БИ-24-1',title='Информация для группы',body='Встреча в учебном офисе в пятницу')
        self.post('/staff/grades',user_id='1',subject='Новая дисциплина',kind='Экзамен',grade='5')
        self.assertEqual(self.post('/staff/grades',user_id='1',subject='Новая дисциплина',kind='Экзамен',grade='Зачёт').status_code,400)
        self.logout();self.login('student1')
        self.assertIn('Новая дисциплина'.encode(),self.client.get('/grades').data)
        self.assertIn('Информация для группы'.encode(),self.client.get('/notices').data)
        nid=self.select("SELECT id FROM notices WHERE title='Информация для группы' AND user_id=1")[0]['id']
        self.post(f'/notices/{nid}/read')
        self.assertEqual(len(self.select('SELECT * FROM notice_reads WHERE user_id=1 AND notice_id=?',(nid,))),1)
        self.logout();self.login('student2')
        self.assertNotIn('Новая дисциплина'.encode(),self.client.get('/grades').data)
        self.assertNotIn('Информация для группы'.encode(),self.client.get('/notices').data)

    @unittest.skip('Student creation moved to reviewed CSV enrollment; see test_registry_import_flow')
    def test_create_group_and_student_inherit_timetable(self):
        self.login('admin')
        self.post('/admin/users',username='newstudent',full_name='Ирина Тестовая',role='student',
                  group_name='БИ-24-1',password='Temporary123')
        uid=self.select("SELECT id FROM users WHERE username='newstudent'")[0]['id']
        self.logout();self.login('newstudent','Temporary123')
        self.assertEqual(self.client.get('/schedule?day=0').status_code,302)
        self.post('/profile/password',old_password='Temporary123',new_password='SecurePassword098!')
        self.assertIn('Цифровая экономика'.encode(),self.client.get('/schedule?day=0').data)
        self.assertNotIn('Цифровая экономика'.encode(),self.client.get('/grades').data)
        self.logout();self.login('dean')
        lid=self.select("SELECT id FROM group_lessons WHERE group_name='БИ-24-1' ORDER BY id LIMIT 1")[0]['id']
        self.post(f'/staff/lesson/{lid}',starts='09:00',ends='10:30',room='909')
        self.logout();self.login('newstudent','SecurePassword098!')
        self.assertIn('909'.encode(),self.client.get('/schedule?day=0').data)
        self.assertIn('Расписание группы обновлено'.encode(),self.client.get('/notices').data)
        self.logout();self.login('student1')
        self.assertIn('909'.encode(),self.client.get('/schedule?day=0').data)

    @unittest.skip('Old manual student creation retired; tested in enrollment and permissions tests')
    def test_admin_blocks_user_and_prevents_self_lockout(self):
        self.login('admin')
        self.post('/admin/users/4/update',role='staff',is_active='0',group_name='')
        self.assertEqual(self.select('SELECT role,is_active FROM users WHERE id=4')[0]['role'],'admin')
        self.post('/admin/users',username='blockedstudent',full_name='Тест Блокировки',role='student',group_name='МН-23-2',password='Temporary123')
        uid=self.select("SELECT id FROM users WHERE username='blockedstudent'")[0]['id']
        self.post(f'/admin/users/{uid}/update',role='student',is_active='0',group_name='МН-23-2')
        self.logout();self.login('blockedstudent','Temporary123')
        self.assertEqual(self.client.get('/grades').status_code,302)

    def test_csrf_and_bad_login(self):
        self.client.get('/login')
        self.assertEqual(self.client.post('/login',data={'username':'student1','password':'1234'}).status_code,400)
        self.assertEqual(self.client.post('/login',data={'_csrf':self.csrf(),'username':'student1','password':'wrong'}).status_code,200)
        self.login()
        self.assertEqual(self.client.post('/requests',data={'type':'Другой вопрос'}).status_code,400)
        self.assertEqual(self.client.get('/admin').status_code,403)

    def test_reopen_revokes_old_document(self):
        self.login('student1')
        rid=self.create_request()
        self.logout();self.login('dean')
        self.post(f'/staff/requests/{rid}/action',action='take')
        self.post(f'/staff/requests/{rid}/action',action='complete')
        self.assertEqual(len(self.select('SELECT * FROM request_documents WHERE request_id=?',(rid,))),1)
        self.post(f'/staff/requests/{rid}/action',action='reopen',note='Исправление ошибочно оформленного документа')
        self.assertEqual(len(self.select('SELECT * FROM request_documents WHERE request_id=?',(rid,))),0)
        self.logout();self.login('student1')
        self.assertEqual(self.client.get(f'/requests/{rid}/document').status_code,404)
        self.assertIn('В работе'.encode(),self.client.get(f'/requests/{rid}').data)

    def test_group_edit_synchronises_profiles(self):
        self.login('admin')
        gid=self.select("SELECT id FROM academic_groups WHERE name='БИ-24-1'")[0]['id']
        self.post(f'/admin/groups/{gid}/update',faculty='Факультет цифровых технологий',
                  program='Информационные системы',course='3')
        student=self.select("SELECT program,course FROM users WHERE username='student1'")[0]
        self.assertEqual(student['program'],'Информационные системы')
        self.assertEqual(student['course'],3)
        self.logout();self.login('student1')
        self.assertIn('Информационные системы'.encode(),self.client.get('/profile').data)

    def test_group_lesson_overlap_is_rejected(self):
        self.login('dean')
        before=len(self.select("SELECT * FROM group_lessons WHERE group_name='БИ-24-1'"))
        self.post('/staff/schedule/add',group_name='БИ-24-1',weekday='0',slot_id='1',week_type='odd',
                  subject='Конфликтующая пара',teacher='Тестовый преподаватель',room='111')
        after=len(self.select("SELECT * FROM group_lessons WHERE group_name='БИ-24-1'"))
        self.assertEqual(before,after)

    def test_init_db_is_idempotent(self):
        before=len(self.select('SELECT * FROM group_lessons'))
        with application.app.app_context():
            application.init_db()
        after=len(self.select('SELECT * FROM group_lessons'))
        self.assertEqual(before,after)


    def test_schedule_uses_real_dates_and_week_parity(self):
        self.login('student1')
        november=self.client.get('/schedule?date=2026-11-23')
        self.assertEqual(november.status_code,200)
        self.assertIn('23 ноября'.encode(),november.data)
        self.assertIn('ЧЁТНАЯ'.encode(),november.data)
        self.assertNotIn('↗ Аудитория'.encode(),november.data)
        # v6: cards are buttons opening detailed lesson dialogs (old class: calendar-lesson).
        self.assertIn(b'class="session-card',november.data)
        self.assertIn(b'data-lesson-dialog=',november.data)

    def test_staff_navigation_is_separate_and_protected(self):
        self.login('dean')
        for url in ('/staff','/staff/schedule','/staff/academic','/staff/announcements'):
            self.assertEqual(self.client.get(url).status_code,200,url)
        self.logout();self.login('student1')
        self.assertEqual(self.client.get('/staff/schedule').status_code,403)

    def test_single_date_override_not_repeated_on_following_weeks(self):
        self.login('dean')
        self.post('/staff/schedule/exception',group_name='БИ-24-1',date='2026-11-23',slot_id='1',
                  action='replace',subject='Цифровая экономика',teacher='В. К. Белова',room='930',
                  reason='Изменение аудитории на конкретную дату')
        self.assertEqual(self.select("SELECT room FROM schedule_overrides WHERE date='2026-11-23'")[0]['room'],'930')
        self.logout();self.login('student1')
        self.assertIn('930'.encode(),self.client.get('/schedule?date=2026-11-23').data)
        self.assertNotIn('930'.encode(),self.client.get('/schedule?date=2026-11-30').data)
        self.assertIn('Открыть дату'.encode(),self.client.get('/').data)

    def test_week_copy_preserves_other_source_and_is_explicit(self):
        self.login('dean')
        self.post('/staff/schedule/add',group_name='БИ-24-1',weekday='0',slot_id='4',
                  week_type='odd',subject='Проектный семинар',teacher='И. В. Сидорова',room='307')
        self.assertEqual(self.post('/staff/schedule/copy',group_name='БИ-24-1',source='odd').status_code,400)
        self.post('/staff/schedule/copy',group_name='БИ-24-1',source='odd',confirm='yes')
        self.assertEqual(self.select("SELECT week_type FROM group_lessons WHERE subject='Проектный семинар'")[0]['week_type'],'both')

    def test_editing_one_parity_does_not_erase_other_week(self):
        self.login('dean')
        lesson=self.select("SELECT id,slot_id,weekday FROM group_lessons WHERE group_name='БИ-24-1' ORDER BY id LIMIT 1")[0]
        self.post(f"/staff/lesson/{lesson['id']}",slot_id=str(lesson['slot_id']),weekday=str(lesson['weekday']),
                  week_type='odd',subject='Новый предмет по нечётным',teacher='Е. А. Смирнова',room='601')
        pair=self.select('SELECT week_type,subject FROM group_lessons WHERE group_name=? AND weekday=? AND slot_id=?',
                         ('БИ-24-1',lesson['weekday'],lesson['slot_id']))
        self.assertEqual({p['week_type'] for p in pair},{'even','odd'})
        self.assertTrue(any(p['subject']=='Новый предмет по нечётным' for p in pair))

    def test_bulk_registry_requires_preview_then_activation(self):
        self.login('admin')
        data='username,full_name,group_name\nregistry.demo,Demo Student,БИ-24-1\n'.encode()
        response=self.client.post('/admin/import',data={'_csrf':self.csrf(),
            'registry':(io.BytesIO(data),'registry.csv')},content_type='multipart/form-data',follow_redirects=True)
        self.assertEqual(response.status_code,200)
        self.assertIn('registry.demo'.encode(),response.data)
        batch=self.select('SELECT id FROM import_batches ORDER BY id DESC LIMIT 1')[0]['id']
        self.assertEqual(len(self.select("SELECT id FROM users WHERE username='registry.demo'")),0)
        result=self.post(f'/admin/import/{batch}/confirm')
        self.assertIn(b'activation_url',result.data)
        student=self.select("SELECT id,is_active FROM users WHERE username='registry.demo'")[0]
        self.assertEqual(student['is_active'],0)
        import csv
        text=io.StringIO(result.data.decode('utf-8-sig'))
        activation=next(csv.DictReader(text))['activation_url']
        token=activation.split('/activate/')[1]
        self.logout()
        self.assertEqual(self.client.get('/activate/'+token).status_code,200)
        self.post('/activate/'+token,password='StrongInvitePass42')
        self.assertEqual(self.select('SELECT is_active FROM users WHERE id=?',(student['id'],))[0]['is_active'],1)
        self.assertEqual(self.login('registry.demo','StrongInvitePass42').status_code,200)
        self.assertEqual(self.client.get('/schedule').status_code,200)

    def test_reject_invalid_registry_without_partial_create(self):
        self.login('admin')
        data=b'username,full_name,group_name\nunknown.demo,Demo Student,NOT_A_GROUP\n'
        response=self.client.post('/admin/import',data={'_csrf':self.csrf(),
            'registry':(io.BytesIO(data),'registry.csv')},content_type='multipart/form-data',follow_redirects=True)
        self.assertIn('не найдена'.encode(),response.data)
        batch=self.select('SELECT id FROM import_batches ORDER BY id DESC LIMIT 1')[0]['id']
        self.post(f'/admin/import/{batch}/confirm')
        self.assertEqual(len(self.select("SELECT id FROM users WHERE username='unknown.demo'")),0)

    def test_current_semester_and_archive_clear(self):
        self.login('student1')
        content=self.client.get('/grades').data
        self.assertIn('2026/27'.encode(),content)
        self.assertIn('2025/26'.encode(),content)
        self.assertIn('Текущий семестр'.encode(),content)

    def test_bell_mark_all_read(self):
        self.login('student1')
        self.assertIn('bell-menu'.encode(),self.client.get('/').data)
        self.post('/notices/read-all')
        unread=self.select('SELECT COUNT(*) AS n FROM notices n WHERE n.user_id=1 AND NOT EXISTS '
            '(SELECT 1 FROM notice_reads r WHERE r.notice_id=n.id AND r.user_id=1)')[0]['n']
        self.assertEqual(unread,0)


if __name__=='__main__':
    unittest.main()
