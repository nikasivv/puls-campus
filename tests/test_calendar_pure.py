"""Independent tests for calendar parity and registry validation; no Flask required."""
import io
import os
import sqlite3
import sys
import unittest
from datetime import date, timedelta
sys.path.insert(0,os.path.dirname(os.path.dirname(__file__)))
from schedule_engine import week_start, week_kind, week_events, conflict
from import_helpers import parse_registry


class CoreCalendarTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.conn.row_factory=sqlite3.Row
        self.conn.executescript('''
        CREATE TABLE bell_slots(id INTEGER PRIMARY KEY,number INTEGER,starts TEXT,ends TEXT);
        CREATE TABLE group_lessons(id INTEGER PRIMARY KEY,group_name TEXT,weekday INTEGER,slot_id INTEGER,
            starts TEXT,ends TEXT,subject TEXT,teacher TEXT,room TEXT,changed INTEGER,week_type TEXT);
        CREATE TABLE schedule_overrides(id INTEGER PRIMARY KEY,group_name TEXT,date TEXT,slot_id INTEGER,
            action TEXT,subject TEXT,teacher TEXT,room TEXT,reason TEXT);
        INSERT INTO bell_slots VALUES(1,1,'09:00','10:30');
        INSERT INTO group_lessons VALUES(1,'А-1',0,1,'09:00','10:30','Аналитика','О. Петров','301',0,'odd');
        ''')

    def tearDown(self):self.conn.close()

    def test_parity_is_iso_based(self):
        self.assertEqual(week_kind(date(2026,11,16)),'odd') # ISO week 47
        self.assertEqual(week_kind(date(2026,11,23)),'even') # ISO week 48
        self.assertEqual(week_start(date(2026,11,22)),date(2026,11,16))

    def test_odd_vs_even_without_invented_classes(self):
        odd=week_events(self.conn,'А-1',date(2026,11,16))
        even=week_events(self.conn,'А-1',date(2026,11,23))
        self.assertEqual(odd[0]['active_count'],1)
        self.assertEqual(even[0]['active_count'],0)

    def test_on_both_weeks_conflicts_with_exclusive(self):
        self.assertTrue(conflict(self.conn,'А-1',0,1,'both'))
        self.assertFalse(conflict(self.conn,'А-1',0,1,'even'))

    def test_date_override_does_not_leak_into_next_week(self):
        self.conn.execute("INSERT INTO schedule_overrides VALUES(1,'А-1','2026-11-16',1,'replace','Проектирование','И. Котов','326','перенос аудитории')")
        revised=week_events(self.conn,'А-1',date(2026,11,16))[0]['lessons'][0]
        self.assertEqual(revised['room'],'326')
        self.assertTrue(revised['exception'])
        next_odd=week_events(self.conn,'А-1',date(2026,11,30))[0]['lessons'][0]
        self.assertEqual(next_odd['room'],'301')

    def test_cancel_visible_but_not_counted(self):
        self.conn.execute("INSERT INTO schedule_overrides VALUES(2,'А-1','2026-11-16',1,'cancel','','','','преподаватель болеет')")
        col=week_events(self.conn,'А-1',date(2026,11,16))[0]
        self.assertEqual(col['active_count'],0)
        self.assertTrue(col['lessons'][0]['cancelled'])

    def test_extra_class_does_not_mutate_template(self):
        self.conn.execute("INSERT INTO schedule_overrides VALUES(3,'А-1','2026-11-23',1,'extra','Семинар','И. Котов','302','дополнительная встреча')")
        self.assertEqual(week_events(self.conn,'А-1',date(2026,11,23))[0]['active_count'],1)
        self.assertEqual(week_events(self.conn,'А-1',date(2026,12,7))[0]['active_count'],0)


class RegistryTests(unittest.TestCase):
    def test_utf8_russian_headings(self):
        rows=parse_registry('Логин;ФИО;Группа\nivan.demo;Иван Иванов;А-1\n'.encode())
        self.assertEqual(rows[0]['username'],'ivan.demo')
        self.assertEqual(rows[0]['errors'],[])

    def test_duplicates_are_flagged(self):
        rows=parse_registry(b'username,full_name,group_name\nivan.demo,Ivan Ivanov,A-1\nivan.demo,Ivan Petrov,A-1\n')
        self.assertTrue(rows[1]['errors'])

    def test_reject_bad_files(self):
        for text in (b'hello\nworld\n',b'',b'username,full_name,group_name\n'):
            with self.assertRaises(ValueError):parse_registry(text)

if __name__=='__main__':unittest.main()
