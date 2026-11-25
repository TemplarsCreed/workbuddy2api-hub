"""One background worker for bounded, versioned dashboard snapshots."""
import collections
import threading
import time


class UsageBoard:
    def __init__(self, size, maintain, build, report_error, preview=None):
        self.size = size
        self.maintain = maintain
        self.build = build
        self.report_error = report_error
        self.preview = preview
        self.stop = threading.Event()
        self.entries = collections.OrderedDict()
        self.condition = threading.Condition()
        self.version = time.time_ns()
        self.progress = (0, 0)
        self.thread = threading.Thread(target=self._run, name="usage-board", daemon=True)
        self.archive_thread = threading.Thread(
            target=self._maintain, name="usage-archive", daemon=True)

    def start(self):
        self.thread.start()
        self.archive_thread.start()

    def close(self):
        self.stop.set()
        with self.condition:
            self.condition.notify()
        self.thread.join(timeout=2)
        self.archive_thread.join(timeout=2)

    def request(self, key, revision, matrix_revision, *, preview_revision=None,
                closed=False, force=False):
        now = time.monotonic()
        with self.condition:
            entry = self.entries.get(key)
            if entry is None:
                if len(self.entries) >= 8:
                    self.entries.popitem(last=False)
                entry = self.entries[key] = {
                    "core": None, "matrix": None, "revision": None,
                    "preview": None, "preview_revision": None,
                    "matrix_revision": None, "updated_at": None, "checked": 0,
                    "matrix_at": 0, "pending": False, "running": False,
                    "error": False, "force": False,
                }
            self.entries.move_to_end(key)
            if (not entry["pending"] and not entry["running"]
                    and (force or (entry["core"] is None and
                                   (not entry["error"] or now - entry["checked"] >= 5))
                         or (not closed and now - entry["checked"] >= 4))):
                entry["pending"] = True
                entry["force"] = force
                self.condition.notify()
            processed, total = self.progress
            reply = {
                "pending": entry["pending"] or entry["running"],
                "error": entry["error"], "indexed_bytes": processed,
                "total_bytes": total, "updated_at": entry["updated_at"],
                "revision": entry["revision"], "matrix_revision": entry["matrix_revision"],
            }
            if entry["core"] is None and entry["preview"] is not None:
                reply["preview_revision"] = entry["preview_revision"]
                if preview_revision != entry["preview_revision"]:
                    reply["preview"] = entry["preview"]
            if entry["core"] is not None and revision != entry["revision"]:
                reply["core"] = entry["core"]
            if entry["matrix"] is not None and matrix_revision != entry["matrix_revision"]:
                reply["matrix"] = entry["matrix"]
            return reply

    def _set_progress(self, offset, total):
        with self.condition:
            self.progress = offset, total

    def _maintain(self):
        while not self.stop.is_set():
            try:
                self.maintain(self.stop)
            except OSError as exc:
                self.report_error(exc)
            self.stop.wait(5)

    def _run(self):
        while not self.stop.is_set():
            now = time.monotonic()
            with self.condition:
                job = next(((key, item) for key, item in self.entries.items()
                            if item["pending"]), None)
                if job is None:
                    self.condition.wait(timeout=1)
                    continue
                key, item = job
                item["running"], item["pending"] = True, False
            try:
                stamp = self.size()
                if self.preview and item["core"] is None:
                    try:
                        preview = self.preview(key, self.stop)
                        if preview is not None:
                            with self.condition:
                                self.version += 1
                                item.update(preview=preview, preview_revision=str(self.version))
                    except Exception as exc:
                        self.report_error(exc)
                day = time.strftime("%Y-%m-%d")
                matrix = (item["matrix"] is None or item["force"]
                          or day != item.get("day") or now - item["matrix_at"] >= 30)
                if (item["core"] is None or item["force"] or matrix
                        or stamp != item.get("stamp")):
                    core, performance = self.build(key, matrix, self.stop, self._set_progress)
                    if self.stop.is_set():
                        return
                    with self.condition:
                        if core != item["core"]:
                            self.version += 1
                            item.update(core=core, revision=str(self.version), preview=None)
                        if matrix:
                            if performance != item["matrix"]:
                                self.version += 1
                                item.update(matrix=performance, matrix_revision=str(self.version))
                            item.update(matrix_at=now, day=day)
                        item.update(stamp=stamp, updated_at=time.time())
                with self.condition:
                    item["error"] = False
            except Exception as exc:
                self.report_error(exc)
                with self.condition:
                    item["error"] = True
            finally:
                with self.condition:
                    item.update(running=False, checked=time.monotonic())
