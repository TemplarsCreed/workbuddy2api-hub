#!/usr/bin/env python3
"""Rotation preserves the logical ledger, its indexes, and existing counters."""
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

with tempfile.TemporaryDirectory(prefix="usage-segments-import-") as startup:
    with patch.dict(os.environ, {"ACCOUNTS_DIR": startup, "WB_PROXY_USAGE_DIR": startup}):
        import wb_proxy as proxy

import wb_usage_io as ledger
from wb_usage_board import UsageBoard


class LedgerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = os.path.join(temp.name, "usage.jsonl")
        for obj, key, value in [
            (proxy, "USAGE_DIR", temp.name),
            (proxy, "USAGE_LOG", self.path),
            (proxy, "ACCOUNTS_DIR", temp.name),
            (proxy, "_usage_snap_state", {}),
            (proxy, "_analytics_state", {}),
            (proxy, "_series_state", {}),
        ]:
            mock = patch.object(obj, key, value)
            mock.start()
            self.addCleanup(mock.stop)
        env = patch.dict(os.environ, {"WB_USAGE_CACHE": "0", "WB_USAGE_DAY_BUCKETS": "1"})
        env.start()
        self.addCleanup(env.stop)

    def append(self, at, tokens=10):
        row = {"at": at, "total_tokens": tokens, "prompt_tokens": tokens,
               "model": "test-model", "account": "acct", "key": "test-key",
               "outcome": "completed", "credit": 1, "realm": "intl"}
        raw = json.dumps(row)
        self.assertGreater(ledger.append_line(self.path, raw), 0)
        return (raw + "\n").encode()

    def test_rotation_keeps_checkpoints_and_daily_limits(self):
        now = time.time()
        first = self.append(now - 1)
        key = ledger.log_key(self.path)
        tail = proxy._log_tail_signature(len(first))
        self.assertTrue(ledger.rotate(self.path, 1))
        ledger.maintain_archive(self.path, 1, threading.Event())
        self.assertEqual(ledger.log_key(self.path), key)
        self.assertEqual(proxy._log_tail_signature(len(first)), tail)
        second = self.append(now, 25)
        with ledger.open_log(self.path) as handle:
            self.assertEqual(handle.read(), first + second)
            handle.seek(len(first))
            self.assertEqual(handle.readline(), second)
        state = {"tokens": {}, "credits": {}, "models": {}, "keytokens": {}}
        totals, end = proxy._scan_daily_usage(0, state)
        self.assertEqual(totals["tokens"], {"acct": 35})
        self.assertEqual(totals["keytokens"], {"test-key": 35})
        self.assertEqual(end, len(first + second))

    def test_archive_index_uses_timestamp_extrema_not_append_order(self):
        with patch.object(ledger, "_INDEX_BLOCK_BYTES", 100):
            first = self.append(100)
            self.append(900)
            self.append(50, 30)
            self.assertTrue(ledger.rotate(self.path, 1))
            ledger.maintain_archive(self.path, 1, threading.Event())
            self.assertEqual(len(list(Path(self.path + ".segments").glob("*.gz"))), 1)
            with ledger.open_log(self.path) as src:
                src.seek(-8, os.SEEK_END)
                self.assertTrue(src.read().endswith(b"\n"))
                src.seek(0)
                self.assertEqual(src.readline(), first)
        blocks = list(ledger.iter_blocks(self.path, 50, 50))
        self.assertEqual(len(blocks), 1)
        self.assertGreater(blocks[0][0], 0)
        result = proxy._scan_usage_analytics_window(None, 50, 50)
        self.assertEqual(result["summary"]["total_tokens"], 30)
        self.assertEqual(proxy._log_complete_frontier(), ledger.log_key(self.path)[0])
        dest = self.path + ".export"
        ledger.export_log(self.path, dest)
        with self.assertRaises(FileExistsError):
            ledger.export_log(self.path, dest)
        with ledger.open_log(self.path) as src:
            self.assertEqual(Path(dest).read_bytes(), src.read())

    def test_unfinished_line_is_never_sealed(self):
        with open(self.path, "wb") as handle:
            handle.write(b'{"at": 1}')
        self.assertFalse(ledger.rotate(self.path, 1))
        self.assertFalse(ledger.has_archive(self.path))

    def test_writes_and_rotation_can_overlap_without_losing_rows(self):
        done = threading.Event()
        counts = []

        def produce(worker):
            for i in range(150):
                text = json.dumps({"at": i, "id": "%s:%s" % (worker, i)})
                counts.append(ledger.append_line(self.path, text))

        writers = [threading.Thread(target=produce, args=(i,)) for i in range(3)]
        for writer in writers:
            writer.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and any(w.is_alive() for w in writers):
            ledger.rotate(self.path, 900)
            ledger.maintain_archive(self.path, 900, done)
        for writer in writers:
            writer.join()
        self.assertEqual(len(counts), 450)
        self.assertTrue(all(counts))
        with ledger.open_log(self.path) as source:
            rows = [json.loads(line)["id"] for line in source]
        self.assertEqual(len(rows), 450)
        self.assertEqual(set(rows), {"%s:%s" % (w, i) for w in range(3) for i in range(150)})

    def test_batch_is_shared_and_historical_edges_are_inclusive(self):
        start = proxy._local_midnight(days_back=5)
        stamps = [proxy._local_midnight(start, -i) + 300 for i in range(6)]
        for i, stamp in enumerate(stamps, 1):
            self.append(stamp, i)
        parsed = []
        read = proxy._read_usage_rows

        def count(*args, **kwargs):
            if kwargs.get("capture") is not None:
                parsed.append(args[0])
            return read(*args, **kwargs)

        with patch.object(proxy, "_BOARD_SCAN_BYTES", 375), patch.object(
                proxy, "_read_usage_rows", side_effect=count):
            proxy._warm_usage_board(threading.Event(), lambda *_: None)
        self.assertGreater(len(parsed), 1)
        self.assertEqual(parsed, sorted(set(parsed)))
        lo, hi = stamps[1], stamps[4]
        exact = proxy._scan_usage_analytics_window(None, lo, hi)
        with patch.object(proxy._usage_scan_local, "board_window", True, create=True):
            merged = proxy._compute_usage_analytics_uncached(since=lo, until=hi)
        self.assertEqual(merged["summary"]["window"]["total_tokens"], 14)
        self.assertEqual(merged["summary"]["window"]["credit"],
                         exact["summary"]["credit"])
        series = proxy._board_series(lo, hi, 86400)
        self.assertEqual(sum(day["total_tokens"] for day in series["series"]), 14)

    def test_restart_restores_three_folds_after_compression(self):
        now = time.time()
        first = self.append(now - 2)
        persist = {"WB_USAGE_CACHE": "1", "WB_USAGE_CACHE_MIN_BYTES": "0"}
        with patch.dict(os.environ, persist), patch.object(proxy, "USAGE_BOARD", object()):
            proxy._warm_usage_board(threading.Event(), lambda *_: None)
            self.assertTrue(ledger.rotate(self.path, 1))
            ledger.maintain_archive(self.path, 1, threading.Event())
            second = self.append(now, 25)
            proxy._usage_snap_state.clear()
            proxy._analytics_state.clear()
            proxy._series_state.clear()
            proxy._usage_cache_forget()
            reader = proxy._read_usage_rows
            with patch.object(proxy, "_read_usage_rows", wraps=reader) as calls:
                proxy._warm_usage_board(threading.Event(), lambda *_: None)
            captures = [call.args[0] for call in calls.call_args_list
                        if call.kwargs.get("capture") is not None]
            self.assertEqual(captures, [len(first)])
            self.assertEqual(proxy._series_state[None]["offset"], len(first + second))
            totals = proxy._analytics_state[None]["maps"]["summary"]["total_tokens"]
            self.assertEqual(totals, 35)

    def test_cold_rebuild_reuses_seals_and_repairs_only_a_bad_summary(self):
        midnight = proxy._local_midnight()
        self.append(midnight - 86400, 9)
        self.append(midnight - 86400 + 3600, 12)
        self.assertTrue(ledger.rotate(self.path, 1))
        ledger.maintain_archive(self.path, 1, threading.Event())
        self.append(midnight + 60, 15)
        self.append(midnight - 86400 + 3600, 4)
        self.assertTrue(ledger.rotate(self.path, 1))
        ledger.maintain_archive(self.path, 1, threading.Event())
        tail = self.append(midnight + 61, 6)
        entries = ledger.sealed_segments(self.path)
        reset = lambda: (proxy._usage_snap_state.clear(), proxy._analytics_state.clear(),
                         proxy._series_state.clear(), proxy._usage_cache_forget())
        with patch.dict(os.environ, {"WB_USAGE_CACHE": "1"}), patch.object(
                proxy, "USAGE_BOARD", object()), patch.object(proxy, "_usage_cache_maybe_save"):
            stop = threading.Event()
            proxy._warm_usage_board(stop, lambda *_: None)
            for entry in entries:
                plain = entry[4]
                self.assertTrue(os.path.isfile(plain + ".rollup.gz"))
                self.assertTrue(proxy._archive_rollup_valid(ledger.read_rollup(plain),
                    proxy._archive_rollup_context(plain), proxy._pricing_inputs()))
            first = proxy._compute_usage_analytics_uncached()
            reset()
            with patch.object(proxy, "_read_usage_rows", wraps=proxy._read_usage_rows) as read:
                proxy._warm_usage_board(stop, lambda *_: None)
                parsed = [call.args[0] for call in read.call_args_list
                          if call.args[2] > call.args[0]]
            self.assertEqual(parsed, [ledger.log_key(self.path)[0] - len(tail)])
            self.assertEqual(proxy._compute_usage_analytics_uncached(), first)
            self.assertEqual(first["summary"]["all_time"]["total_tokens"], 46)
            self.assertEqual(proxy._series_state[None]["days"][proxy._local_day_key(
                midnight - 3600)]["total_tokens"], 25)

            plain = entries[1][4]
            summary_path = Path(plain + ".rollup.gz")
            incomplete = ledger.read_rollup(plain)
            del incomplete["states"][0]["hours_floor_at"]
            invalid_deflate = bytes.fromhex("1f8b08000000000000ff070000000000000000")
            for damage in ("header", "deflate", "missing field"):
                with self.subTest(damage=damage):
                    if damage == "missing field":
                        ledger.write_rollup(plain, incomplete)
                    else:
                        summary_path.write_bytes(b"corrupted" if damage == "header" else invalid_deflate)
                    reset()
                    with patch.object(proxy, "_read_usage_rows", wraps=proxy._read_usage_rows) as read:
                        proxy._warm_usage_board(stop, lambda *_: None)
                        parsed = [call.args[0] for call in read.call_args_list
                                  if call.args[2] > call.args[0]]
                    self.assertEqual(parsed, [entries[1][0], ledger.log_key(self.path)[0] - len(tail)])
                    self.assertEqual(proxy._compute_usage_analytics_uncached(), first)

    def test_today_preview_has_no_accumulated_value(self):
        now = time.time()
        today = proxy._local_midnight(now)
        self.append(today - 5, 100)
        self.assertTrue(ledger.rotate(self.path, 1))
        ledger.maintain_archive(self.path, 1, threading.Event())
        self.append(today, 15)
        payload = proxy._preview_usage_today(("today", None, None), threading.Event())
        self.assertEqual(payload["summary"]["window"]["total_tokens"], 15)
        self.assertNotIn("all_time", payload["summary"])
        self.assertIsNone(proxy._preview_usage_today(("all", None, None), threading.Event()))

    def test_recent_version_avoids_reloading_and_changes_on_append(self):
        self.append(time.time() - 1, 1)
        key = (10, "all", 1, None, None, None, None)
        with patch.object(proxy, "_recent_replies", proxy.OrderedDict()), patch.object(
                proxy, "recent_usage", wraps=proxy.recent_usage) as read:
            first = proxy._versioned_recent_reply(key, None)
            same = proxy._versioned_recent_reply(key, first["revision"])
            self.assertEqual(same, {"revision": first["revision"], "unchanged": True})
            self.assertEqual(read.call_count, 1)
            self.append(time.time(), 2)
            changed = proxy._versioned_recent_reply(key, first["revision"])
            self.assertNotEqual(changed["revision"], first["revision"])
            self.assertEqual(changed["rows"][0]["total_tokens"], 2)
            self.assertEqual(read.call_count, 2)


class BoardTests(unittest.TestCase):
    def test_preview_is_published_before_the_history_job_completes(self):
        entered = threading.Event()
        release = threading.Event()

        def build(*args):
            entered.set()
            release.wait(3)
            return {"complete": True}, {}

        board = UsageBoard(lambda: 123, lambda stop: None, build, self.fail,
                           preview=lambda key, stop: {"today": 10})
        board.start()
        self.addCleanup(board.close)
        self.addCleanup(release.set)
        board.request("today", "", "")
        self.assertTrue(entered.wait(1))
        interim = board.request("today", "", "")
        self.assertEqual(interim["preview"], {"today": 10})
        self.assertTrue(interim["pending"])
        self.assertNotIn("core", interim)
        polled = board.request("today", "", "", preview_revision=interim["preview_revision"])
        self.assertNotIn("preview", polled)

    def test_background_build_and_revision_only_poll(self):
        release = threading.Event()
        builds = []
        errors = []

        def build(key, matrix, stop, progress):
            builds.append(key)
            release.wait(timeout=2)
            progress(20, 20)
            return {"analytics": 7}, {"perf": 12}

        board = UsageBoard(lambda: 20, lambda stop: None, build, errors.append)
        board.start()
        self.addCleanup(board.close)
        first = board.request("test", "", "", closed=True)
        self.assertTrue(first["pending"])
        self.assertNotIn("core", first)
        release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            result = board.request("test", "", "", closed=True)
            if not result["pending"]:
                break
            time.sleep(.01)
        self.assertEqual(result["core"], {"analytics": 7})
        self.assertEqual(result["indexed_bytes"], 20)
        same = board.request("test", result["revision"], result["matrix_revision"], closed=True)
        self.assertNotIn("core", same)
        self.assertNotIn("matrix", same)
        self.assertEqual(builds, ["test"])
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
