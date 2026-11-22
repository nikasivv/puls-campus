"""Session types, multi-teacher rooms and groups, tested with pure sqlite3."""
import os
import sqlite3
import sys
import unittest
from datetime import date
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)))
from lesson_meta import items_from_text, metadata_from_form, load_items
from schedule_engine import week_events


class MetadataTests(unittest.TestCase):
    def test_multiple_teachers_and_rooms(self):
        payload=metadata_from_form({'lesson_type':'ЛК','teachers':'Р. Г. Большин; С. Р. Грушина',
                                    'rooms':'А-13; В-78'})
        self.assertEqual(payload['teachers'],['Р. Г. Большин','С. Р. Грушина'])
        self.assertEqual(payload['rooms'],['А-13','В-78'])
        self.assertEqual(load_items(payload['rooms_json']),['А-13','В-78'])

    def test_invalid_lecture_data_is_rejected(self):
        for payload in ({'lesson_type':'XXX','teachers':'A','rooms':'111'},
                        {'lesson_type':'ЛК','teachers':'','rooms':'111'},
                        {'lesson_type':'ПР','teachers':'A','rooms':''},
                        {'lesson_type':'ПР','teachers':'A','rooms':';'.join(str(i) for i in range(20))}):
            with self.assertRaises(ValueError): metadata_from_form(payload)


class GroupedCalendarTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row
        self.conn.executescript('''
          CREATE TABLE bell_slots (id INTEGER PRIMARY KEY,number INTEGER,starts TEXT,ends TEXT);
          CREATE TABLE group_lessons (id INTEGER PRIMARY KEY,group_name TEXT,weekday INTEGER,
            slot_id INTEGER,starts TEXT,ends TEXT,subject TEXT,teacher TEXT,room TEXT,
            changed INTEGER,week_type TEXT,lesson_type TEXT,teachers_json TEXT,rooms_json TEXT,shared_id TEXT);
          CREATE TABLE schedule_overrides (id INTEGER PRIMARY KEY,group_name TEXT,date TEXT,slot_id INTEGER,
            action TEXT,subject TEXT,teacher TEXT,room TEXT,reason TEXT,lesson_type TEXT,
            teachers_json TEXT,rooms_json TEXT,shared_id TEXT);
          INSERT INTO bell_slots VALUES (1,1,'09:00','10:30');
          INSERT INTO group_lessons VALUES
          (1,'БИ-24-1',0,1,'09:00','10:30','Большие данные','Большин','А-13',0,'both','ЛК',
            '["Большин","Грушина"]','["А-13","В-78"]','shared-lecture'),
          (2,'БИ-24-2',0,1,'09:00','10:30','Большие данные','Большин','А-13',0,'both','ЛК',
            '["Большин","Грушина"]','["А-13","В-78"]','shared-lecture');
        ''')

    def tearDown(self):self.conn.close()

    def test_one_shared_lecture_visible_in_each_group(self):
        for group in ('БИ-24-1','БИ-24-2'):
            lesson=week_events(self.conn,group,date(2026,11,16))[0]['lessons'][0]
            self.assertEqual(lesson['groups'],['БИ-24-1','БИ-24-2'])
            self.assertEqual(lesson['lesson_type'],'ЛК')
            self.assertEqual(lesson['lesson_type_name'],'Лекция')
            self.assertEqual(lesson['teachers'],['Большин','Грушина'])
            self.assertEqual(lesson['rooms'],['А-13','В-78'])
            self.assertEqual((lesson['starts'],lesson['ends']),('09:00','10:30'))

    def test_override_only_one_group_does_not_claim_another_group_attends(self):
        self.conn.execute('''INSERT INTO schedule_overrides VALUES
        (1,'БИ-24-2','2026-11-16',1,'replace','Другая пара','Лектор','315',
         'замена','ПР','["Лектор"]','["315"]',NULL)''')
        first=week_events(self.conn,'БИ-24-1',date(2026,11,16))[0]['lessons'][0]
        second=week_events(self.conn,'БИ-24-2',date(2026,11,16))[0]['lessons'][0]
        self.assertEqual(first['groups'],['БИ-24-1'])
        self.assertEqual(second['groups'],['БИ-24-2'])
        self.assertEqual(second['subject'],'Другая пара')
        future=week_events(self.conn,'БИ-24-2',date(2026,11,23))[0]['lessons'][0]
        self.assertEqual(future['groups'],['БИ-24-1','БИ-24-2'])

    def test_cancel_for_one_group_does_not_cancel_another(self):
        self.conn.execute('''INSERT INTO schedule_overrides VALUES
        (3,'БИ-24-2','2026-11-16',1,'cancel','','','','перенос пары',
         'ПР','[]','[]',NULL)''')
        a=week_events(self.conn,'БИ-24-1',date(2026,11,16))[0]['lessons'][0]
        b=week_events(self.conn,'БИ-24-2',date(2026,11,16))[0]['lessons'][0]
        self.assertEqual(a['groups'],['БИ-24-1'])
        self.assertEqual(b['groups'],['БИ-24-2'])
        self.assertFalse(a['cancelled'])
        self.assertTrue(b['cancelled'])

    def test_linked_date_override(self):
        for idx,group in enumerate(('БИ-24-1','БИ-24-2'),1):
            self.conn.execute('''INSERT INTO schedule_overrides VALUES
            (?,?,'2026-11-16',1,'replace','Другая лекция','Лектор','315',
             'перенос','ЛК','["Лектор"]','["315"]','one-day-shared')''',(idx,group))
        for group in ('БИ-24-1','БИ-24-2'):
            lesson=week_events(self.conn,group,date(2026,11,16))[0]['lessons'][0]
            self.assertEqual(lesson['groups'],['БИ-24-1','БИ-24-2'])
            self.assertTrue(lesson['exception'])

if __name__=='__main__':unittest.main()
