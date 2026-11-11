"""Configurable background schedule hours (#456), defaults unchanged."""
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_scheduler
import wb_settings


class StubPool(object):
    def __init__(self, directory):
        self.dir = directory
        self.accounts = []


class ScheduleSettingsTests(unittest.TestCase):
    def test_defaults_when_the_key_is_absent(self):
        with tempfile.TemporaryDirectory(prefix="sched-") as directory:
            self.assertEqual(wb_settings.schedule_hours(directory), {
                "checkin": [], "travel": [],
                "keepalive": [22], "cat": [1],
            })

    def test_list_and_comma_string_are_accepted(self):
        with tempfile.TemporaryDirectory(prefix="sched-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": "20, 10,10", "travel": [6, 18],
                "keepalive": "12", "cat": [1],
            })
            hours = wb_settings.schedule_hours(directory)
            self.assertEqual(hours["checkin"], [10, 20])
            self.assertEqual(hours["travel"], [6, 18])
            self.assertEqual(hours["keepalive"], [12])

    def test_invalid_empty_and_out_of_range_fall_back_per_key(self):
        with tempfile.TemporaryDirectory(prefix="sched-") as directory:
            data = wb_settings.load(directory)
            data["schedule"] = {
                "checkin": [24, -1, "x"],   # nothing valid -> default
                "travel": [],                 # empty -> default
                "keepalive": "99",            # out of range -> default
                "cat": [3, 3],                # valid -> kept
            }
            wb_settings.save(directory, data)
            hours = wb_settings.schedule_hours(directory)
            self.assertEqual(hours["checkin"], [])
            self.assertEqual(hours["travel"], [])
            self.assertEqual(hours["keepalive"], [22])
            self.assertEqual(hours["cat"], [3])

    def test_partial_update_keeps_other_keys(self):
        with tempfile.TemporaryDirectory(prefix="sched-") as directory:
            wb_settings.set_schedule_hours(directory, {"checkin": [10, 20]})
            wb_settings.set_schedule_hours(directory, {"keepalive": [12]})
            hours = wb_settings.schedule_hours(directory)
            self.assertEqual(hours["checkin"], [10, 20])
            self.assertEqual(hours["keepalive"], [12])
            self.assertEqual(hours["travel"], [])

    def test_invalid_write_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="sched-") as directory:
            with self.assertRaises(ValueError):
                wb_settings.set_schedule_hours(directory, {"checkin": []})
            with self.assertRaises(ValueError):
                wb_settings.set_schedule_hours(directory, {"cat": "26"})
            with self.assertRaises(ValueError):
                wb_settings.set_schedule_hours(directory, "9,21")


class SchedulerScheduleTests(unittest.TestCase):
    def test_scheduler_picks_up_changes_without_restart(self):
        with tempfile.TemporaryDirectory(prefix="sched-run-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": [10, 20], "travel": [10, 20],
                "keepalive": [12], "cat": [1],
            })
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            self.assertEqual(scheduler.checkin_hours, [10, 20])
            self.assertEqual(scheduler.keepalive_hours, [12])
            self.assertEqual(scheduler.all_hours, [1, 10, 12, 20])
            self.assertIn("10:00/20:00", scheduler.status()["mode_cn"])
            self.assertIn("12:00", scheduler.status()["mode_intl"])

            wb_settings.set_schedule_hours(directory, {"checkin": [8]})
            self.assertTrue(scheduler._reload_schedule())
            self.assertEqual(scheduler.checkin_hours, [8])
            self.assertIn("08:00", scheduler.status()["mode_cn"])


class FakeAccount(object):
    def __init__(self, realm="cn"):
        self.uid = "fake-uid-1234"
        self.realm = realm
        self.tasks_enabled = True
        self.account_banned = False
        self.expires_at = 0.0
        self.last_error = ""
        self.checkins = 0
        self.reports = 0
        self.daily = 0
        self.refreshes = 0

    def task_active(self):
        return self.tasks_enabled

    def can_checkin(self):
        return True

    def checkin(self, trigger=""):
        self.checkins += 1
        return {"ok": True, "msg": "OK"}

    def can_report_activity(self):
        return True

    def report_activity(self):
        self.reports += 1
        return {"ok": True, "msg": "OK"}

    def can_daily_chat(self):
        return True

    def daily_chat(self, trigger=""):
        self.daily += 1
        return {"ok": True, "msg": "OK"}

    def refresh(self):
        self.refreshes += 1
        return True


class TaskGatingTests(unittest.TestCase):
    """A configured hour means that task, not "run the whole routine".

    #515: the 01:00 cat slot used to run check-in, activity report and cat
    travel as well, so a schedule of 10:00/20:00 still checked in at 01:00.
    """

    def _run_at(self, scheduler, account, hour, force=False):
        stamp = time.struct_time((2026, 10, 20, hour, 0, 0, 0, 0, -1))
        seen = {"travel": 0, "bonus": 0, "cat": 0}

        def travel(_acc):
            seen["travel"] += 1
            return {}

        def bonus(_acc):
            seen["bonus"] += 1
            return {}

        def night(_acc):
            seen["cat"] += 1
            return {"logs": []}

        with mock.patch.object(wb_scheduler.time, "localtime", lambda *a: stamp), \
                mock.patch.object(wb_scheduler.time, "sleep", lambda *_: None), \
                mock.patch.object(wb_scheduler, "do_cat_travel", travel), \
                mock.patch.object(wb_scheduler.wb_tasks, "run_streak_bonus", bonus), \
                mock.patch.object(wb_scheduler.wb_tasks, "run_night_growth", night):
            scheduler._run_cycle("test", force=force)
        return seen

    def test_task_flag_off_skips_tasks_but_keeps_keepalive(self):
        with tempfile.TemporaryDirectory(prefix="sched-task-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": [10, 20], "travel": [10, 20],
                "keepalive": [10], "cat": [1],
            })
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            account = FakeAccount()
            account.tasks_enabled = False
            account.expires_at = time.time() + 60
            scheduler.pool.accounts = [account]

            seen = self._run_at(scheduler, account, 10)
            self.assertEqual((account.checkins, account.reports), (0, 0))
            self.assertEqual((seen["travel"], seen["bonus"], seen["cat"]),
                             (0, 0, 0))
            self.assertEqual(account.refreshes, 1)

    def test_task_flag_on_still_runs_checkin(self):
        with tempfile.TemporaryDirectory(prefix="sched-task-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": [10, 20], "travel": [10, 20],
                "keepalive": [12], "cat": [1],
            })
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            account = FakeAccount()
            scheduler.pool.accounts = [account]

            self._run_at(scheduler, account, 10)
            self.assertEqual((account.checkins, account.reports), (1, 1))

    def test_cat_hour_does_not_run_checkin_or_travel(self):
        with tempfile.TemporaryDirectory(prefix="sched-gate-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": [10, 20], "travel": [10, 20],
                "keepalive": [12], "cat": [1],
            })
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            account = FakeAccount()
            scheduler.pool.accounts = [account]

            seen = self._run_at(scheduler, account, 1)
            self.assertEqual((account.checkins, account.reports), (0, 0))
            self.assertEqual((seen["travel"], seen["bonus"], seen["cat"]), (0, 0, 1))

    def test_checkin_hour_runs_checkin_and_report_but_not_travel(self):
        with tempfile.TemporaryDirectory(prefix="sched-gate-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": [10, 20], "travel": [9, 21],
                "keepalive": [12], "cat": [1],
            })
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            account = FakeAccount()
            scheduler.pool.accounts = [account]

            seen = self._run_at(scheduler, account, 10)
            self.assertEqual((account.checkins, account.reports), (1, 1))
            self.assertEqual((seen["travel"], seen["bonus"], seen["cat"]), (0, 0, 0))

    def test_manual_trigger_forces_every_task(self):
        with tempfile.TemporaryDirectory(prefix="sched-gate-") as directory:
            wb_settings.set_schedule_hours(directory, {
                "checkin": [10, 20], "travel": [10, 20],
                "keepalive": [12], "cat": [1],
            })
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            account = FakeAccount()
            scheduler.pool.accounts = [account]

            seen = self._run_at(scheduler, account, 3, force=True)
            self.assertEqual((account.checkins, account.reports), (1, 1))
            self.assertEqual((seen["travel"], seen["bonus"], seen["cat"]), (1, 1, 1))


class LogRoutingTests(unittest.TestCase):
    """Scheduler logs must land in the wb_proxy that is actually running.

    Started as `python wb_proxy.py`, the main module is `__main__`; a plain
    `import wb_proxy` loads a second copy whose LOG_STORE is None, so every
    scheduler line was dropped. Measured on a real install: 64k log rows and
    zero `[调度器]` entries, while the startup banner had 8.
    """

    def test_log_goes_to_the_running_main_module(self):
        recorded = []

        class FakeMain(object):
            LOG_BUFFER = []

            @staticmethod
            def add_log_entry(text, tag=None):
                recorded.append((text, tag))

        fake_main = FakeMain()
        with mock.patch.dict(sys.modules, {"__main__": fake_main}):
            self.assertIs(wb_scheduler._proxy_module(), fake_main)
            with tempfile.TemporaryDirectory(prefix="sched-log-") as directory:
                scheduler = wb_scheduler.Scheduler(StubPool(directory))
                scheduler.log("巡检开始")

        self.assertEqual(recorded, [("[调度器] 巡检开始", "scheduler")])

    def test_log_falls_back_when_main_module_is_not_the_proxy(self):
        # Under `python -m pytest` or an import from another host, `__main__`
        # is the test runner: it must not be mistaken for the gateway.
        cached = sys.modules.get("wb_proxy") or wb_scheduler._proxy_module()
        with mock.patch.dict(sys.modules, {"__main__": object()}):
            self.assertIs(wb_scheduler._proxy_module(), cached)



class CnRewardsRandomSlotTests(unittest.TestCase):
    """国内版签到 + 猫猫旅行：每天 08:00–10:00 之间随机一个时刻，不再占整点。"""

    def test_default_scheduler_uses_a_random_slot_instead_of_hours(self):
        with tempfile.TemporaryDirectory(prefix="sched-cn-") as directory:
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            self.assertEqual((scheduler.checkin_hours, scheduler.travel_hours), ([], []))
            self.assertRegex(scheduler.cn_rewards_slot, r"^0[89]:[0-5]\d$")
            # 随机时刻不进整点网格：8 点 / 9 点不会再来一次整点巡检。
            self.assertNotIn(8, scheduler.all_hours)
            self.assertNotIn(9, scheduler.all_hours)
            status = scheduler.status()
            self.assertIn("每日随机时段", status["mode_cn"])
            self.assertIn(scheduler.cn_rewards_slot, status["mode_cn"])
            self.assertEqual(status["cn_rewards_slot"], scheduler.cn_rewards_slot)

    def test_slot_is_drawn_once_per_day_and_persisted(self):
        with tempfile.TemporaryDirectory(prefix="sched-cn-") as directory:
            first = wb_settings.cn_rewards_random_time(directory)
            self.assertEqual(wb_settings.cn_rewards_random_time(directory), first,
                             "同一天重读必须拿到同一个时刻")
            stored = wb_settings.load(directory).get(wb_settings.CN_REWARDS_RANDOM_KEY)
            self.assertEqual(stored["time"], first)
            self.assertEqual(stored["date"], time.strftime("%Y-%m-%d", time.localtime()))
            hour, minute = [int(part) for part in first.split(":")]
            self.assertGreaterEqual(hour * 60 + minute, 8 * 60)
            self.assertLess(hour * 60 + minute, 10 * 60)

            tomorrow = time.time() + 86400
            nxt = wb_settings.cn_rewards_random_time(directory, now=tomorrow)
            hour, minute = [int(part) for part in nxt.split(":")]
            self.assertGreaterEqual(hour * 60 + minute, 8 * 60)
            self.assertLess(hour * 60 + minute, 10 * 60)

    def test_slot_fires_once_after_the_draw_and_only_inside_the_grace_window(self):
        with tempfile.TemporaryDirectory(prefix="sched-cn-") as directory:
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            scheduler.cn_rewards_slot = "08:37"

            def at(hour, minute):
                return time.struct_time((2026, 11, 9, hour, minute, 0, 0, 0, -1))

            self.assertEqual(scheduler._due_cn_rewards_slot(at(8, 36)), "",
                             "还没到点不能跑")
            self.assertEqual(scheduler._due_cn_rewards_slot(at(8, 37)), "08:37")
            self.assertEqual(scheduler._due_cn_rewards_slot(at(9, 30)), "",
                             "当天只命中一次")
            scheduler._cn_rewards_done.clear()
            self.assertEqual(scheduler._due_cn_rewards_slot(at(12, 30)), "",
                             "超出补跑窗口就不再跑")

    def test_random_slot_cycle_runs_checkin_and_travel_but_not_keepalive_or_cat(self):
        with tempfile.TemporaryDirectory(prefix="sched-cn-") as directory:
            scheduler = wb_scheduler.Scheduler(StubPool(directory))
            account = FakeAccount()
            account.expires_at = time.time() + 60
            scheduler.pool.accounts = [account]
            stamp = time.struct_time((2026, 11, 9, 8, 40, 0, 0, 0, -1))
            seen = {"travel": 0, "cat": 0}

            def travel(_acc):
                seen["travel"] += 1
                return {}

            def night(_acc):
                seen["cat"] += 1
                return {"logs": []}

            with mock.patch.object(wb_scheduler.time, "localtime", lambda *a: stamp), \
                    mock.patch.object(wb_scheduler.time, "sleep", lambda *_: None), \
                    mock.patch.object(wb_scheduler, "do_cat_travel", travel), \
                    mock.patch.object(wb_scheduler.wb_tasks, "run_streak_bonus", lambda _a: {}), \
                    mock.patch.object(wb_scheduler.wb_tasks, "run_night_growth", night):
                scheduler._run_cycle("国内版任务随机时段命中", cn_rewards_only=True)

            self.assertEqual((account.checkins, account.reports), (1, 1))
            self.assertEqual(seen, {"travel": 1, "cat": 0})
            self.assertEqual(account.refreshes, 0, "随机时段那一轮不碰 Token 保活")

if __name__ == "__main__":
    unittest.main()
