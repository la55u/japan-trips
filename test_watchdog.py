import unittest
from datetime import datetime, timedelta, timezone

import watchdog

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def run(*, status="completed", conclusion="success", created_minutes=30, age_minutes=5):
    return {
        "status": status,
        "conclusion": conclusion,
        "createdAt": (NOW - timedelta(minutes=created_minutes)).isoformat(),
        "updatedAt": (NOW - timedelta(minutes=age_minutes)).isoformat(),
        "url": "https://example.test/run",
    }


class WatchdogDecisionTests(unittest.TestCase):
    def evaluate(self, runs):
        return watchdog.evaluate_runs(
            runs,
            NOW,
            max_age=timedelta(minutes=75),
            retry_cooldown=timedelta(minutes=30),
        )

    def test_fresh_success_is_not_dispatched(self):
        action, reason = self.evaluate([run(age_minutes=20)])
        self.assertEqual(action, "skip")
        self.assertIn("20m old", reason)

    def test_stale_success_is_dispatched(self):
        action, reason = self.evaluate([run(created_minutes=100, age_minutes=90)])
        self.assertEqual(action, "dispatch")
        self.assertIn("90m old", reason)

    def test_active_run_suppresses_dispatch(self):
        action, reason = self.evaluate(
            [run(status="in_progress", conclusion="", age_minutes=90)]
        )
        self.assertEqual(action, "skip")
        self.assertIn("already in_progress", reason)

    def test_recent_failure_respects_retry_cooldown(self):
        runs = [
            run(
                conclusion="failure",
                created_minutes=10,
                age_minutes=5,
            ),
            run(created_minutes=120, age_minutes=100),
        ]
        action, reason = self.evaluate(runs)
        self.assertEqual(action, "skip")
        self.assertIn("10m old", reason)

    def test_old_failure_after_stale_success_is_dispatched(self):
        runs = [
            run(conclusion="failure", created_minutes=40, age_minutes=35),
            run(created_minutes=120, age_minutes=100),
        ]
        action, _ = self.evaluate(runs)
        self.assertEqual(action, "dispatch")

    def test_no_runs_is_dispatched(self):
        action, reason = self.evaluate([])
        self.assertEqual(action, "dispatch")
        self.assertIn("no successful", reason)


if __name__ == "__main__":
    unittest.main()
