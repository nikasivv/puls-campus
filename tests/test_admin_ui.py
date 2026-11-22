"""Regression checks for v6.3 templates and admin-role policy, no Flask needed."""
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[1]

class TestVisualRegression(unittest.TestCase):
    def test_demo_selector_not_last_child(self):
        css=(ROOT/'static/style.css').read_text(encoding='utf-8')
        self.assertNotIn('.demo-account>span:last-child{font-size:23px}',css)
        self.assertIn('.demo-account>span:first-child',css)
    def test_scorecard_has_small_text_style(self):
        html=(ROOT/'templates/home.html').read_text(encoding='utf-8')
        self.assertIn('class="metric-word"',html)
    def test_admin_tabs_are_distinct(self):
        html=(ROOT/'templates/admin.html').read_text(encoding='utf-8')
        self.assertIn("admin_tab=='import'",html)
        self.assertIn("admin_tab=='users'",html)
        self.assertIn("admin_tab=='journal'",html)
    def test_no_manual_student_creation(self):
        py=(ROOT/'app.py').read_text(encoding='utf-8')
        section=py[py.index('def admin_create_user():'):py.index("@app.route('/admin/users/<int:uid>/update'")]
        self.assertIn("role not in ('staff','admin')",section)
        self.assertNotIn("INSERT INTO academic_groups",section)

if __name__=='__main__':unittest.main()
