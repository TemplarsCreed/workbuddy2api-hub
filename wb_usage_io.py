# -*- coding: utf-8 -*-
"""Atomic JSONL append and a seekable view across sealed usage segments.

## 为什么单独一个模块

`usage.jsonl` 是账本：每一行都是一次真实调用。它可能在**多个进程**里被同时追加
（网关本体 + 维护脚本 + 面板导出任务）。原先的写法是

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")

文本模式按缓冲区切分，一次 `.write()` 会落成多个 `write(2)`，两个人一起写就会拼出
半行（实测出现过一条 `9.8}`）。实测两种表现：

- 4 个进程各写 120 行：480 行只落进去 408~430 行；
- 单进程 8 线程各写 300 行：2400 行只落进去 2224 行（丢 7.3%）——**这才是网关的
  真实形态**（每个请求各写一行）。

丢行比坏行更危险：读的一侧全部 `except: continue`，所以丢掉的调用既不在请求数里、
也不在积分合计里，账面上直接少一笔，且没有任何报错。

## 做法

1. **单次 `os.write`**：整行先编码成 bytes，再一次系统调用写完，不给交叉留缝隙；
2. **`O_APPEND`**：由内核把写入位置定在末尾；
3. **进程锁**：追加和轮转共用一个独立的 `.lock` 文件，避免写入已封存的段；
4. **进程内线程锁**：同进程多线程先串起来，减少跨进程争抢。
"""
import bisect
import gzip
import io
import json
import math
import os
import re
import shutil
import tempfile
import threading
import time
import zlib

_LOCK_SUFFIX = ".lock"
_THREAD_LOCK = threading.Lock()
_CAN_ROTATE = True

try:                                                    # Windows
    import msvcrt

    def _lock(lock_fd):
        # LK_LOCK 重试 10 秒后抛 OSError；账本争抢是毫秒级的，够用。
        while True:
            try:
                msvcrt.locking(lock_fd, msvcrt.LK_LOCK, 1)
                return
            except OSError:
                # 拿不到就继续等：这里没有别的退路，放弃等于丢账。
                continue

    def _unlock(lock_fd):
        try:
            msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

except ImportError:
    try:
        import fcntl
    except ImportError:
        fcntl = None
        _CAN_ROTATE = False

    def _lock(lock_fd):
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)

    def _unlock(lock_fd):
        if fcntl is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


_ARCHIVE = re.compile(r"(\d{20})\.(\d+)\.(\d+)\.(\d+)\.jsonl(?:\.gz)?")
_archive_cache = {}
_bounds_cache = {}
_archive_lock = threading.Lock()
_INDEX_BLOCK_BYTES = 1024 * 1024


def _invalidate_archives(path):
    with _archive_lock:
        _archive_cache.pop(path, None)


def _archives(path):
    directory = path + ".segments"
    with _archive_lock:
        try:
            st = os.stat(directory)
            stamp = (st.st_dev, st.st_ino, st.st_mtime_ns)
        except FileNotFoundError:
            _archive_cache.pop(path, None)
            return ()
        cached = _archive_cache.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        entries = {}
        for name in os.listdir(directory):
            match = _ARCHIVE.fullmatch(name)
            if match is None:
                continue
            start, dev, ino, size = map(int, match.groups())
            plain = os.path.join(directory, name.removesuffix(".gz"))
            entries[start] = (start, size, dev, ino, plain)
        entries = tuple(entries[k] for k in sorted(entries))
        end = 0
        for start, size, _, _, _ in entries:
            if start != end or size <= 0:
                raise OSError("usage archive has a missing or invalid segment")
            end += size
        _archive_cache[path] = (stamp, entries)
        return entries


def _active_stat(path):
    try:
        st = os.stat(path)
        return st.st_size, st.st_dev, st.st_ino
    except FileNotFoundError:
        return None


def _layout(path):
    # Rotation publishes the old file by renaming it. Retry if the active
    # identity changed while the archive directory was being read.
    while True:
        active = _active_stat(path)
        entries = _archives(path)
        after = _active_stat(path)
        if ((active[1:] if active else None)
                == (after[1:] if after else None)):
            break
    end = entries[-1][0] + entries[-1][1] if entries else 0
    if after is not None:
        return entries + ((end, *after, path),)
    return entries


def log_key(path):
    entries = _layout(path)
    if not entries:
        return (0, 0, 0)
    start, size = entries[-1][:2]
    return (start + size, *entries[0][2:4])


def active_stamp(path):
    try:
        st = os.stat(path)
        return st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns
    except FileNotFoundError:
        return None


def sealed_segments(path):
    return _archives(path)


def archive_stamp(plain):
    compressed = active_stamp(plain + ".gz")
    return [active_stamp(plain), compressed]


def read_rollup(plain):
    try:
        with gzip.open(plain + ".rollup.gz", "rt", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, EOFError, RecursionError, zlib.error):
        return None


def write_rollup(plain, value):
    fd, tmp = tempfile.mkstemp(prefix=".rollup-", dir=os.path.dirname(plain))
    try:
        with os.fdopen(fd, "wb") as fileobj:
            with gzip.GzipFile(fileobj=fileobj, mode="wb", compresslevel=2, mtime=0) as out:
                with io.TextIOWrapper(out, encoding="utf-8") as writer:
                    json.dump(value, writer, ensure_ascii=True, allow_nan=False,
                              separators=(",", ":"))
        os.replace(tmp, plain + ".rollup.gz")
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _open_archive(path):
    try:
        return open(path, "rb")
    except FileNotFoundError:
        return gzip.open(path + ".gz", "rb")


def _sync_directory(path):
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class _SegmentReader(io.RawIOBase):
    def __init__(self, path, entries):
        self.path = path
        self.entries = entries
        self.starts = [seg[0] for seg in entries]
        self.size = entries[-1][0] + entries[-1][1]
        self.pos = 0
        self.handle = None
        self.backing = None
        self.index = None
        self.base = 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, position, whence=os.SEEK_SET):
        if whence == os.SEEK_CUR:
            position += self.pos
        elif whence == os.SEEK_END:
            position += self.size
        elif whence != os.SEEK_SET:
            raise ValueError("invalid whence")
        if position < 0:
            raise ValueError("negative seek position")
        self.pos = position
        return position

    def _close_segment(self):
        if self.handle is not None:
            self.handle.close()
        if self.backing is not None:
            self.backing.close()
        self.handle = self.backing = None
        self.index = None

    def _open(self, index, offset):
        start, size, dev, ino, path = self.entries[index]
        if path == self.path:
            try:
                handle = open(path, "rb")
                st = os.fstat(handle.fileno())
                if (st.st_dev, st.st_ino) == (dev, ino):
                    return handle
                handle.close()
            except FileNotFoundError:
                pass
            for seg in _archives(path):
                if seg[0] == start and seg[1] >= size and seg[2:4] == (dev, ino):
                    size, path = seg[1], seg[4]
                    break
            else:
                raise OSError("usage log changed during read")
        try:
            return open(path, "rb")
        except FileNotFoundError:
            pass
        blocks = _index_blocks(path, size)
        self.backing = open(path + ".gz", "rb")
        self.base = 0
        if blocks:
            # Gzip members begin at the same boundaries as the time index.
            i = bisect.bisect_right([block[0] for block in blocks], offset)
            encoded_start = blocks[i][3]
            if encoded_start is not None:
                self.base = blocks[i-1][0] if i else 0
                self.backing.seek(encoded_start)
        return gzip.GzipFile(fileobj=self.backing, mode="rb")

    def readinto(self, buffer):
        if self.pos >= self.size or not buffer:
            return 0
        index = bisect.bisect_right(self.starts, self.pos) - 1
        start, size = self.entries[index][:2]
        offset = self.pos - start
        if self.index != index or (self.backing is not None and not (
                0 <= offset - self.base - self.handle.tell() <= _INDEX_BLOCK_BYTES)):
            self._close_segment()
            self.base = 0
            self.handle = self._open(index, offset)
            self.index = index
        relative = offset - self.base
        if self.handle.tell() != relative:
            self.handle.seek(relative)
        count = self.handle.readinto(memoryview(buffer)[:size - offset])
        if not count:
            raise OSError("usage segment truncated during read")
        self.pos += count
        return count

    def close(self):
        self._close_segment()
        super().close()


def has_archive(path):
    return os.path.isdir(path + ".segments")


def open_log(path, mode="rb", encoding=None):
    entries = _layout(path)
    if not entries:
        raise FileNotFoundError(path)
    binary = io.BufferedReader(_SegmentReader(path, entries))
    return binary if mode == "rb" else io.TextIOWrapper(binary, encoding=encoding or "utf-8")


def scan_boundary(path, offset, size, budget):
    if size - offset <= budget:
        return size
    with open_log(path, "rb") as handle:
        handle.seek(offset + budget - 1)
        handle.readline()
        return min(size, handle.tell())


def rotate(path, max_bytes):
    """Seal an active file at a complete line boundary, under the append lock."""
    if not _CAN_ROTATE or max_bytes <= 0 or (_active_stat(path) or (0,))[0] < max_bytes:
        return False
    with _THREAD_LOCK:
        lock_fd = os.open(path + _LOCK_SUFFIX, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            _lock(lock_fd)
            active = _active_stat(path)
            if not active or active[0] < max_bytes:
                return False
            size, dev, ino = active
            with open(path, "rb") as handle:
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    return False
            archives = _archives(path)
            start = archives[-1][0] + archives[-1][1] if archives else 0
            directory = path + ".segments"
            os.makedirs(directory, exist_ok=True)
            name = "%020d.%d.%d.%d.jsonl" % (start, dev, ino, size)
            try:
                os.replace(path, os.path.join(directory, name))
            except PermissionError:
                # A Windows reader may still have the active segment open.
                return False
            _invalidate_archives(path)
            return True
        finally:
            _unlock(lock_fd)
            os.close(lock_fd)


def _index_and_compress(path, size, compress, stop):
    fd, tmp = tempfile.mkstemp(prefix=".usage-", dir=os.path.dirname(path))
    try:
        blocks = []
        start = end = 0
        earliest, latest = math.inf, -math.inf
        certain = True
        with os.fdopen(fd, "wb") as target, _open_archive(path) as source:
            archive = None
            encoded_start = None
            try:
                for raw in source:
                    if stop.is_set():
                        return
                    if compress:
                        if archive is None:
                            encoded_start = target.tell()
                            archive = gzip.GzipFile(
                                fileobj=target, mode="wb", compresslevel=3, mtime=0)
                        archive.write(raw)
                    end += len(raw)
                    try:
                        row = json.loads(raw.decode("utf-8"))
                        at = row.get("at", 0)
                        if not isinstance(at, (int, float)) or not math.isfinite(at):
                            certain = False
                        else:
                            earliest, latest = min(earliest, at), max(latest, at)
                    except (UnicodeError, ValueError, AttributeError, OverflowError, RecursionError):
                        certain = False
                    if end - start >= _INDEX_BLOCK_BYTES:
                        blocks.append([end, earliest if certain else None,
                                       latest if certain else None, encoded_start])
                        if archive is not None:
                            archive.close()
                            archive = None
                        start = end
                        earliest, latest, certain = math.inf, -math.inf, True
                if end > start:
                    blocks.append([end, earliest if certain else None,
                                   latest if certain else None, encoded_start])
            finally:
                if archive is not None:
                    archive.close()
            if end != size:
                raise OSError("usage archive length changed while indexing")
            if compress:
                target.flush()
                os.fsync(target.fileno())
            else:
                target.write(json.dumps(blocks, allow_nan=False).encode("ascii"))
        if compress:
            os.replace(tmp, path + ".gz")
            _write_index(path, blocks)
            _sync_directory(os.path.dirname(path))
            try:
                os.unlink(path)
            except PermissionError:
                pass  # Windows readers can finish using the plain copy.
        else:
            os.replace(tmp, path + ".idx")
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _write_index(path, blocks):
    fd, tmp = tempfile.mkstemp(prefix=".usage-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="ascii") as target:
            json.dump(blocks, target, allow_nan=False)
        os.replace(tmp, path + ".idx")
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def maintain_archive(path, max_bytes, stop):
    if not _CAN_ROTATE or not has_archive(path):
        return
    lock_fd = os.open(path + ".archive.lock", os.O_RDWR | os.O_CREAT, 0o644)
    try:
        _lock(lock_fd)
        _maintain_archive(path, max_bytes, stop)
    finally:
        _unlock(lock_fd)
        os.close(lock_fd)


def _maintain_archive(path, max_bytes, stop):
    for _, size, _, _, plain in _archives(path):
        if stop.is_set():
            return
        compressed = os.path.isfile(plain + ".gz")
        indexed = os.path.isfile(plain + ".idx")
        raw = os.path.isfile(plain)
        if compressed and indexed and raw:
            _sync_directory(os.path.dirname(plain))
            try:
                os.unlink(plain)
            except PermissionError:
                pass
        if not indexed or (raw and not compressed and max_bytes > 0):
            compress = max_bytes > 0 and raw
            _index_and_compress(plain, size, compress, stop)
            return


def iter_blocks(path, since, until, offset=0, stop_at=None):
    """Return byte intervals for a time query, keeping unknown blocks."""
    for start, size, _, _, plain in _layout(path):
        if start + size <= offset:
            continue
        if stop_at is not None and start >= stop_at:
            break
        if plain == path or (since is None and until is None):
            yield start, start + size
            continue
        blocks = _index_blocks(plain, size)
        if blocks is None:
            yield start, start + size
            continue
        lo = 0
        for hi, first, last, _ in blocks:
            if start + hi > offset and (first is None or last is None
                    or ((since is None or last >= since)
                        and (until is None or first <= until))):
                yield start + lo, start + hi
            lo = hi


def _index_blocks(path, size):
    try:
        st = os.stat(path + ".idx")
        stamp = st.st_mtime_ns, st.st_size
        cached = _bounds_cache.get(path)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        with open(path + ".idx", encoding="ascii") as handle:
            blocks = json.load(handle)
        previous = 0
        previous_encoded = -1
        for end, first, last, encoded in blocks:
            if not isinstance(end, int) or not previous < end <= size:
                return None
            if ((first is None) != (last is None)
                    or (first is not None and not (
                        math.isfinite(first) and math.isfinite(last) and first <= last))):
                return None
            if encoded is not None:
                if not isinstance(encoded, int) or encoded <= previous_encoded:
                    return None
                previous_encoded = encoded
            previous = end
        if previous != size:
            return None
        _bounds_cache[path] = (stamp, blocks)
        return blocks
    except (OSError, TypeError, ValueError, OverflowError):
        return None


def rotation_bytes():
    try:
        return max(0, int(os.environ.get("WB_USAGE_ROTATE_BYTES", 8 * 1024 * 1024)))
    except ValueError:
        return 8 * 1024 * 1024


def append_line(path, text, error_hook=None):
    """把 `text`（一行，不含换行）原子地追加到 `path`，返回写入字节数。

    失败时调 `error_hook(message)` 而不是抛出：账本写不进去不该让请求失败
    （调用方原本也是 try/except 包着的），但必须留痕。
    """
    payload = (text.rstrip("\n") + "\n").encode("utf-8")
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with _THREAD_LOCK:
            lock_fd = os.open(path + _LOCK_SUFFIX, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                _lock(lock_fd)
                try:
                    # O_APPEND：内核负责定位到末尾；单次 write 不给交错留缝。
                    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                                 | getattr(os, "O_BINARY", 0), 0o644)
                    try:
                        written = 0
                        while written < len(payload):
                            count = os.write(fd, payload[written:])
                            if not count:
                                raise OSError("usage append made no progress")
                            written += count
                    finally:
                        os.close(fd)
                finally:
                    _unlock(lock_fd)
            finally:
                os.close(lock_fd)
    except Exception as exc:                            # noqa: BLE001
        if error_hook is not None:
            error_hook(str(exc))
        return 0
    return len(payload)


def export_log(source, destination):
    """Export a consistent byte snapshot for single-file readers."""
    source_path = os.path.normcase(os.path.realpath(source))
    destination_path = os.path.normcase(os.path.realpath(destination))
    if destination_path == source_path or os.path.dirname(destination_path) == source_path + ".segments":
        raise ValueError("export destination must be outside the live ledger")
    with open_log(source) as src, open(destination, "xb") as out:
        try:
            shutil.copyfileobj(src, out)
        except Exception:
            out.close()
            os.unlink(destination)
            raise


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Export the complete usage ledger")
    parser.add_argument("command", choices=["export"])
    parser.add_argument("source", help="active usage.jsonl path")
    parser.add_argument("destination", help="new JSONL file to create")
    args = parser.parse_args()
    export_log(args.source, args.destination)
