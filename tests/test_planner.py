from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from taskagent.planner import plan_day
from taskagent.store import Store


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        base = Path(self.temp.name)
        self.store = Store(base / "db.sqlite3", base / "data")

    def tearDown(self):
        self.temp.cleanup()

    def add_task(self, title: str, *, priority=3, estimate=60, due=None,
                 fixed_start=None, fixed_end=None, status="open") -> str:
        task_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with self.store.transaction() as conn:
            conn.execute(
                """INSERT INTO tasks(id,identity_key,title,description,status,due_at,estimated_minutes,
                                      fixed_start_at,fixed_end_at,priority,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (task_id, f"test:{task_id}", title, "", status, due, estimate,
                 fixed_start, fixed_end, priority, now, now),
            )
        return task_id

    def test_fixed_time_conflict_selects_higher_priority_and_persists_loser(self):
        day = "2099-10-21"
        preferred_id = self.add_task("物产中大元通电缆招聘会", priority=2, estimate=None,
                                     due=f"{day}T17:00:00+08:00",
                                     fixed_start=f"{day}T14:30:00+08:00",
                                     fixed_end=f"{day}T16:00:00+08:00")
        lower_id = self.add_task("另一场冲突招聘会", priority=5, estimate=None,
                                 due=f"{day}T17:00:00+08:00",
                                 fixed_start=f"{day}T14:30:00+08:00",
                                 fixed_end=f"{day}T16:00:00+08:00")
        independent_id = self.add_task("整理投递材料", priority=3, estimate=60)

        result = plan_day(self.store, day, ["14:00-18:00"])

        self.assertEqual(result["solver_status"], "OPTIMAL")
        scheduled_ids = {item["task_id"] for item in result["scheduled"]}
        self.assertIn(preferred_id, scheduled_ids)
        self.assertIn(independent_id, scheduled_ids)
        self.assertNotIn(lower_id, scheduled_ids)
        loser = next(item for item in result["not_scheduled"] if item["task_id"] == lower_id)
        self.assertEqual(loser["decision"], "unselected")
        self.assertIn("物产中大元通电缆招聘会", loser["reason"])
        persisted = self.store.plan_items(result["plan_run_id"])
        self.assertEqual(len(persisted), 3)
        self.assertEqual({item["task_id"] for item in persisted},
                         {preferred_id, lower_id, independent_id})

    def test_missing_estimate_and_busy_window_are_explained(self):
        day = "2099-10-22"
        unknown_id = self.add_task("补充耗时估计", estimate=None)
        blocked_id = self.add_task("准备材料", estimate=90)

        result = plan_day(self.store, day, ["09:00-12:00"], busy=["09:00-12:00=已确定会议"])

        rows = {item["task_id"]: item for item in result["not_scheduled"]}
        self.assertEqual(rows[unknown_id]["decision"], "needs_estimate")
        self.assertEqual(rows[blocked_id]["decision"], "unselected")
        self.assertIn("已确定会议", rows[blocked_id]["reason"])

    def test_due_date_before_planning_day_is_not_scheduled(self):
        task_id = self.add_task("已逾期事项", estimate=30, due="2099-10-20T12:00:00+08:00")
        result = plan_day(self.store, "2099-10-21", ["09:00-12:00"])
        overdue = next(item for item in result["not_scheduled"] if item["task_id"] == task_id)
        self.assertEqual(overdue["decision"], "overdue")


if __name__ == "__main__":
    unittest.main()
