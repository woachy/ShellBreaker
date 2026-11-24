"""
解压引擎抽象层 — 铁律二：黑盒防注入
支持 Bandizip (bz.exe)、WinRAR/UnRAR 与内置/系统 7-Zip (7z.exe / 7za.exe)。
阶段 6.1：手动指定路径优先 + 柔性降级（找不到引擎不崩溃）。
"""
import subprocess
import logging
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from enum import Enum, auto
from typing import Optional, Callable

from core.exceptions import (
    EngineNotFoundError,
    PasswordRequiredError,
    WrongPasswordError,
    ExtractionFailedError,
    ExtractionAbortedError,
)

logger = logging.getLogger("ShellBreaker.Engine")

_PERCENT_RE = re.compile(r"(\d{1,3})\s*%")


class EngineType(Enum):
    BANDIZIP = auto()
    WINRAR = auto()
    SEVENZIP = auto()


# Bandizip 安装目录（用于递归搜索 bz.exe，绝不用 GUI exe 路径）
BANDIZIP_INSTALL_ROOTS = [
    r"C:\Program Files\Bandizip",
    r"C:\Program Files (x86)\Bandizip",
    r"C:\Bandizip",
]

WINRAR_PATHS = [
    r"C:\Program Files\WinRAR\WinRAR.exe",
    r"C:\Program Files (x86)\WinRAR\WinRAR.exe",
    r"C:\WinRAR\WinRAR.exe",
]

UNRAR_PATHS = [
    r"C:\Program Files\WinRAR\UnRAR.exe",
    r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
]

SEVENZIP_PATHS = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files\7-Zip-Zstandard\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
    r"C:\7-Zip\7z.exe",
]


def resolve_bandizip_cli(path_or_dir: str | Path) -> Optional[str]:
    """
    统一解析 Bandizip 命令行程序 (bz.exe / bc.exe)：
    - 若传入路径已是存在的 bz.exe 或 bc.exe，直接返回解析后的绝对路径；
    - 若传入路径是 Bandizip.exe（不区分大小写）或目录：自动查找同级目录及一层子目录下的
      bz.exe（其次 bc.exe），找到则记录 info 日志并返回；找不到返回 None，绝不把 GUI
      Bandizip.exe 当作 CLI 使用。
    """
    if path_or_dir is None:
        return None
    raw_str = str(path_or_dir).strip()
    if not raw_str:
        return None
    p = Path(raw_str)

    def _search_dir(base_dir: Path) -> Optional[str]:
        if not base_dir.is_dir():
            return None
        for cli_name in ("bz.exe", "bc.exe"):
            cand = base_dir / cli_name
            if cand.is_file():
                return str(cand.resolve())
        try:
            subdirs = sorted(
                (entry for entry in base_dir.iterdir() if entry.is_dir()),
                key=lambda d: d.name.lower(),
            )
        except OSError:
            subdirs = []
        for cli_name in ("bz.exe", "bc.exe"):
            for sub in subdirs:
                cand = sub / cli_name
                if cand.is_file():
                    return str(cand.resolve())
        return None

    if p.is_file():
        fname = p.name.lower()
        if fname in ("bz.exe", "bc.exe"):
            return str(p.resolve())
        if fname == "bandizip.exe":
            found = _search_dir(p.parent)
            if found:
                logger.info(f"检测到 Bandizip GUI ({p.name})，已自动纠偏为命令行程序: {found}")
                return found
            return None
        return None

    if p.is_dir():
        found = _search_dir(p)
        if found:
            logger.info(f"从目录 {p} 自动解析 Bandizip CLI: {found}")
            return found
        return None

    return None


def _is_encrypted_zip(archive_path: Path | str) -> bool:
    """
    O(1) 精确判别文件是否为包含加密成员的合法 ZIP（原生兼容 MP4+ZIP 偏移图种）：
    读取中央目录 infolist() 检查任一成员 flag_bits & 0x1 是否置位；
    非 ZIP、损坏假包或 IO 异常安全返回 False，守护 Bug #3 防盲跑密码库红线。
    """
    try:
        with zipfile.ZipFile(archive_path, "r") as zf:
            return any(bool(info.flag_bits & 0x1) for info in zf.infolist())
    except (zipfile.BadZipFile, OSError, ValueError, RuntimeError):
        return False


def _is_header_encrypted_rar(archive_path: Path | str) -> bool:
    """
    O(1) 判别文件是否为加密文件头 (-hp) 的 RAR4 / RAR5 归档：
    - RAR4: MAIN_HEAD (0x73) 的 MHD_PASSWORD 位 (0x0080) 置位；
    - RAR5: 8 字节签名后首个块类型为 4 (Archive Encryption Header)。
    """
    try:
        with open(archive_path, "rb") as f:
            hdr = f.read(32)
        if hdr.startswith(b"Rar!\x1a\x07\x00") and len(hdr) >= 14:
            _crc, head_type, head_flags, _head_size = struct.unpack("<HBHH", hdr[7:14])
            return head_type == 0x73 and bool(head_flags & 0x0080)
        if hdr.startswith(b"Rar!\x1a\x07\x01\x00") and len(hdr) >= 14:
            # 跳过 8B 签名 + 4B head_crc，解析 vint head_size 后读取首个 vint head_type
            pos = 12
            limit = min(len(hdr), pos + 10)
            while pos < limit:
                b = hdr[pos]
                pos += 1
                if not (b & 0x80):
                    break
            else:
                return False
            if pos >= len(hdr):
                return False
            head_type = 0
            shift = 0
            limit_t = min(len(hdr), pos + 10)
            while pos < limit_t:
                b = hdr[pos]
                pos += 1
                head_type |= (b & 0x7F) << shift
                if not (b & 0x80):
                    return head_type == 4
                shift += 7
    except (OSError, struct.error):
        return False
    return False


class BaseEngine:
    """解压引擎抽象基类。"""

    supports_fast_test: bool = False
    _PROBE_CACHE_MAX_SIZE: int = 16

    def __init__(self, exe_path: str | Path):
        self.exe_path = str(Path(exe_path).resolve())
        if not os.path.isfile(self.exe_path):
            raise EngineNotFoundError(self.exe_path)
        self._active_processes: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        self._aborted_task_ids: set[str] = set()
        self._probe_cache: dict[tuple[str, int, int], tuple[Optional[str], bool]] = {}

    def test_password(
        self,
        archive: str | Path,
        password: Optional[str] = None,
        timeout: int = 60,
        task_id: Optional[str] = None,
    ) -> bool:
        """
        极速密码预检（不写盘或仅测单个最小成员）：
        - 返回 True：密码验证正确；
        - 返回 False：密码错误或需要密码；
        - 任务中止：抛出 ExtractionAbortedError；
        - 坏包/非密码类错误：抛出 ExtractionFailedError。
        """
        return False

    @staticmethod
    def _get_probe_cache_key(archive_path: Path) -> Optional[tuple[str, int, int]]:
        try:
            st = archive_path.stat()
            return (str(archive_path), st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _get_cached_probe(
        self, key: Optional[tuple[str, int, int]]
    ) -> Optional[tuple[Optional[str], bool]]:
        if key is None:
            return None
        with self._lock:
            return self._probe_cache.get(key)

    def _set_cached_probe(
        self,
        key: Optional[tuple[str, int, int]],
        value: tuple[Optional[str], bool],
    ) -> None:
        if key is None:
            return
        with self._lock:
            if key not in self._probe_cache and len(self._probe_cache) >= self._PROBE_CACHE_MAX_SIZE:
                oldest_key = next(iter(self._probe_cache))
                self._probe_cache.pop(oldest_key, None)
            self._probe_cache[key] = value

    def extract(
        self,
        archive: str | Path,
        dest: str | Path,
        password: Optional[str] = None,
        timeout: int = 300,
        task_id: Optional[str] = None,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> Path:
        raise NotImplementedError

    def supports_repair_for(self, archive: str | Path) -> bool:
        """当前引擎是否支持对目标归档执行独立修复提取（默认 True 以兼容旧版 Mock 引擎）。"""
        return True

    @staticmethod
    def _is_embedded_polyglot(archive_path: Path) -> bool:
        """检测文件是否为合法的偏移 > 0 内嵌图种（用于快速预检防误判）。"""
        try:
            from core.sandbox import detect_embedded_archive
            emb_ext, emb_offset = detect_embedded_archive(archive_path)
            return emb_ext is not None and emb_offset > 0
        except Exception:
            return False

    def repair_extract(
        self,
        archive: str | Path,
        dest: str | Path,
        password: Optional[str] = None,
        timeout: int = 600,
        task_id: Optional[str] = None,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> Path:
        return self.extract(
            archive, dest, password=password, timeout=timeout,
            task_id=task_id, on_progress=on_progress,
        )

    def terminate_process(self, task_id: str):
        """中止指定任务对应的活跃引擎子进程。"""
        with self._lock:
            self._aborted_task_ids.add(task_id)
            proc = self._active_processes.get(task_id)
        if proc is not None:
            try:
                logger.info(f"正在强行终止任务 [{task_id}] 引擎子进程 (pid={proc.pid})")
                proc.kill()
            except Exception as e:
                logger.warning(f"终止任务 [{task_id}] 子进程异常: {e}")

    def terminate_all(self) -> None:
        """线程安全地终止所有活跃引擎子进程并清理记录（用于关窗/退出清理）。"""
        with self._lock:
            items = list(self._active_processes.items())
            for task_id, _ in items:
                self._aborted_task_ids.add(task_id)
            self._active_processes.clear()
        for task_id, proc in items:
            try:
                logger.info(f"正在强行终止任务 [{task_id}] 引擎子进程 (pid={proc.pid})")
                proc.kill()
            except Exception as e:
                logger.warning(f"终止任务 [{task_id}] 子进程异常: {e}")

    def reset_abort(self, task_id: str):
        """重置任务的中止状态（用于重新解压）。"""
        with self._lock:
            self._aborted_task_ids.discard(task_id)
            self._active_processes.pop(task_id, None)

    def is_aborted(self, task_id: str) -> bool:
        """检查任务是否已被中止。"""
        with self._lock:
            return task_id in self._aborted_task_ids

    @staticmethod
    def _safe_file_size(path: str | Path) -> int:
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    @staticmethod
    def _calc_dir_size(dir_path: Path) -> int:
        """递归安全计算目录当前已写入的总字节数，用于沙箱体积旁路进度估算。"""
        total = 0
        try:
            stack = [str(dir_path)]
            while stack:
                current = stack.pop()
                try:
                    with os.scandir(current) as it:
                        for entry in it:
                            try:
                                if entry.is_dir(follow_symlinks=False):
                                    stack.append(entry.path)
                                elif entry.is_file(follow_symlinks=False):
                                    total += entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                continue
                except OSError:
                    continue
        except Exception:
            pass
        return total

    @staticmethod
    def _decode_output(data: bytes) -> str:
        """自适应解码引擎子进程输出（优先 UTF-8，回退 GB18030，绝不抛出解码异常）。"""
        if not data:
            return ""
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("gb18030", errors="replace")

    def _run_command(
        self,
        cmd: list[str],
        timeout: int,
        task_id: Optional[str] = None,
        on_progress: Optional[Callable[[int], None]] = None,
        monitor_dest: Optional[Path] = None,
        archive_size: int = 0,
    ) -> subprocess.CompletedProcess:
        logger.debug(f"执行命令: {cmd}")
        if task_id:
            with self._lock:
                if task_id in self._aborted_task_ids:
                    raise ExtractionAbortedError(task_id, "任务已被用户中止")

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        if task_id:
            with self._lock:
                if task_id in self._aborted_task_ids:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    raise ExtractionAbortedError(task_id, "任务已被用户中止")
                self._active_processes[task_id] = proc

        stdout_chunks: list[bytes] = []
        stderr_chunks: list[bytes] = []
        progress_lock = threading.Lock()
        has_stdout_progress = False
        last_reported_pct = -1

        def _emit_stream_pct(val: int) -> None:
            nonlocal has_stdout_progress, last_reported_pct
            pct = max(0, min(100, val))
            should_emit = False
            with progress_lock:
                has_stdout_progress = True
                if pct != last_reported_pct:
                    last_reported_pct = pct
                    should_emit = True
            if should_emit and on_progress is not None:
                try:
                    on_progress(pct)
                except Exception as cb_err:
                    logger.debug(f"进度回调异常: {cb_err}")

        def _reader(stream, raw_buf: list[bytes]) -> None:
            pending = ""
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    raw_buf.append(chunk)
                    if on_progress is not None:
                        # 百分比由纯 ASCII 数字和 % 构成，latin-1 零失败解析且避免多字节截断问题
                        text = pending + chunk.decode("latin-1", errors="ignore")
                        parts = re.split(r"[\r\n]+", text)
                        for part in parts[:-1]:
                            for m in _PERCENT_RE.finditer(part):
                                _emit_stream_pct(int(m.group(1)))
                        tail = parts[-1]
                        if "%" in tail:
                            for m in _PERCENT_RE.finditer(tail):
                                _emit_stream_pct(int(m.group(1)))
                            pending = tail[tail.rfind("%") + 1:][-32:]
                        else:
                            pending = tail[-32:]
            except Exception as e:
                logger.debug(f"引擎流读取结束: {e}")

        t_out = threading.Thread(target=_reader, args=(proc.stdout, stdout_chunks), daemon=True)
        t_err = threading.Thread(target=_reader, args=(proc.stderr, stderr_chunks), daemon=True)
        t_out.start()
        t_err.start()

        start_time = time.monotonic()
        last_size_poll = start_time
        retcode = None

        try:
            while True:
                if task_id and self.is_aborted(task_id):
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    raise ExtractionAbortedError(task_id, "任务已被用户中止")

                elapsed = time.monotonic() - start_time
                if timeout is not None and elapsed >= timeout:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    raise subprocess.TimeoutExpired(cmd, timeout)

                wait_slice = 0.1
                if timeout is not None:
                    wait_slice = min(wait_slice, max(0.01, timeout - elapsed))
                try:
                    retcode = proc.wait(timeout=wait_slice)
                    break
                except subprocess.TimeoutExpired:
                    pass

                # 沙箱体积旁路采样（每 250ms 轮询一次，一旦 stdout 出现真实百分比立即停止覆盖）
                if (
                    on_progress is not None
                    and monitor_dest is not None
                    and archive_size > 0
                ):
                    now = time.monotonic()
                    if now - last_size_poll >= 0.25:
                        last_size_poll = now
                        with progress_lock:
                            use_sidechannel = not has_stdout_progress
                        if use_sidechannel:
                            extracted_bytes = self._calc_dir_size(monitor_dest)
                            est_pct = min(99, int(extracted_bytes / max(1, archive_size * 1.15) * 100))
                            est_pct = max(0, est_pct)
                            emit_pct = None
                            with progress_lock:
                                if not has_stdout_progress and est_pct != last_reported_pct:
                                    last_reported_pct = est_pct
                                    emit_pct = est_pct
                            if emit_pct is not None:
                                try:
                                    on_progress(emit_pct)
                                except Exception as cb_err:
                                    logger.debug(f"旁路进度回调异常: {cb_err}")

            # 正常结束，等待流读取线程把缓冲数据读尽
            t_out.join(timeout=2.0)
            t_err.join(timeout=2.0)

            stdout = self._decode_output(b"".join(stdout_chunks))
            stderr = self._decode_output(b"".join(stderr_chunks))

            logger.debug(f"引擎退出码: {retcode}")
            if stdout:
                logger.debug(f"引擎 stdout (前200): {stdout[:200]}")
            if stderr:
                logger.debug(f"引擎 stderr (前200): {stderr[:200]}")

            if task_id and self.is_aborted(task_id):
                raise ExtractionAbortedError(task_id, "任务已被用户中止")

            return subprocess.CompletedProcess(cmd, retcode, stdout, stderr)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=2.0)
            except Exception:
                pass
            t_out.join(timeout=1.0)
            t_err.join(timeout=1.0)
            if task_id and self.is_aborted(task_id):
                raise ExtractionAbortedError(task_id, "任务已被用户中止")
            raise ExtractionFailedError(str(cmd[-1] if cmd else "?"), -1, f"解压超时 ({timeout}s)")
        except ExtractionAbortedError:
            try:
                proc.kill()
            except Exception:
                pass
            try:
                proc.wait(timeout=2.0)
            except Exception:
                pass
            t_out.join(timeout=1.0)
            t_err.join(timeout=1.0)
            raise
        except Exception:
            if task_id and self.is_aborted(task_id):
                raise ExtractionAbortedError(task_id, "任务已被用户中止")
            raise
        finally:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
            if task_id:
                with self._lock:
                    self._active_processes.pop(task_id, None)

    @staticmethod
    def _classify_result(
        result: subprocess.CompletedProcess,
        archive_str: str,
        password_was_provided: bool,
        password_used: str = "",
    ) -> None:
        """
        精准三分异常——绝不允许密码问题被当成文件损坏进入 Fallback 链。

        优先级：
        1. 密码错误 → WrongPasswordError（阻止 Fallback）
        2. 需要密码但未提供 → PasswordRequiredError（触发密码弹窗）
        3. 文件损坏/格式错误 → ExtractionFailedError（允许 Fallback）
        """
        combined = (result.stdout + result.stderr).lower()

        # --- 第一优先级：密码错误关键词 ---
        wrong_pwd_kw = [
            "wrong password", "incorrect password",
            "password incorrect", "bad password",
            "密码错误", "密码不正确",
        ]
        if any(kw in combined for kw in wrong_pwd_kw):
            raise WrongPasswordError(archive_str, password_used, result.stderr or result.stdout)

        # --- 第二优先级：需要密码关键词 ---
        need_pwd_kw = [
            "password", "encrypted", "密码", "加密",
            "no password", "missing password",
        ]
        is_encrypted = any(kw in combined for kw in need_pwd_kw)

        if password_was_provided and is_encrypted:
            # 传了密码但引擎仍然报需要密码 → 密码错误
            raise WrongPasswordError(archive_str, password_used, result.stderr or result.stdout)

        if is_encrypted:
            # 未传密码但引擎报需要密码
            raise PasswordRequiredError(archive_str, result.stderr or result.stdout)

        # --- 第三优先级：非零退出 → 文件损坏 ---
        if result.returncode != 0:
            raise ExtractionFailedError(archive_str, result.returncode, result.stderr)


class BandizipEngine(BaseEngine):
    """Bandizip (bz.exe) 引擎。"""

    supports_fast_test: bool = True

    def __init__(self, exe_path: str | Path):
        raw_path = Path(exe_path)
        if raw_path.name.lower() == "bandizip.exe" or raw_path.is_dir():
            resolved = resolve_bandizip_cli(raw_path)
            if resolved is None:
                raise EngineNotFoundError(
                    f"不能使用图形界面程序 Bandizip.exe，请指定命令行程序 bz.exe ({raw_path})"
                )
            exe_path = resolved
        super().__init__(exe_path)

    _BZ_LIST_LINE_RE = re.compile(
        r"^\s*\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+(\S+)\s+(\d+)\s+\d+\s+(.+?)\s*$"
    )
    _BZ_LIST_LINE_ALT_RE = re.compile(
        r"^\s*\d{4}[-/]\d{2}[-/]\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+(\S+)\s+(\d+)\s+(.+?)\s*$"
    )

    @classmethod
    def _parse_smallest_member(cls, stdout: str) -> Optional[str]:
        """从 bz l 输出中解析体积最小的常规文件路径（过滤目录项）。"""
        if not stdout:
            return None
        candidates: list[tuple[int, str]] = []
        for line in stdout.splitlines():
            m = cls._BZ_LIST_LINE_RE.match(line)
            if not m:
                m = cls._BZ_LIST_LINE_ALT_RE.match(line)
            if not m:
                continue
            attr, size_str, name = m.group(1), m.group(2), m.group(3).strip()
            if not name:
                continue
            if "d" in attr.lower() or name.endswith(("/", "\\")):
                continue
            try:
                size = int(size_str)
            except ValueError:
                continue
            candidates.append((size, name))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0] == 0, item[0]))
        return candidates[0][1]

    def _probe_archive_for_test(
        self,
        archive_path: Path,
        timeout: int = 60,
        task_id: Optional[str] = None,
    ) -> tuple[Optional[str], bool]:
        """
        探测压缩包结构以加速后续密码预检：
        - 加密文件头返回 (None, True)，后续可直接用 `bz l -p:{pwd}` 毫秒级验密；
        - 非加密头返回 (smallest_member, False)，后续优先定向测试单个最小常规文件；
        - 坏包/非密码错误直接抛出 ExtractionFailedError。
        """
        cache_key = self._get_probe_cache_key(archive_path)
        cached = self._get_cached_probe(cache_key)
        if cached is not None:
            return cached

        archive_str = str(archive_path)
        cmd = [str(self.exe_path), "l", "-p:", archive_str]
        result = self._run_command(cmd, timeout=min(timeout, 30), task_id=task_id)

        if result.returncode != 0:
            try:
                self._classify_result(
                    result, archive_str, password_was_provided=False, password_used=""
                )
            except (PasswordRequiredError, WrongPasswordError):
                probed = (None, True)
                self._set_cached_probe(cache_key, probed)
                return probed
            except ExtractionFailedError:
                # 偏移图种在 l 列表探测失败时平滑降级为整包 t 验证，不误判为坏包
                if self._is_embedded_polyglot(archive_path):
                    logger.debug(
                        f"Bandizip 列表探测遇图种偏移包 ({archive_path.name})，平滑降级为整包测试"
                    )
                    probed = (None, False)
                    self._set_cached_probe(cache_key, probed)
                    return probed
                raise

        smallest = self._parse_smallest_member(result.stdout)
        probed = (smallest, False)
        self._set_cached_probe(cache_key, probed)
        return probed

    def test_password(
        self,
        archive: str | Path,
        password: Optional[str] = None,
        timeout: int = 60,
        task_id: Optional[str] = None,
    ) -> bool:
        if task_id and self.is_aborted(task_id):
            raise ExtractionAbortedError(task_id, "任务已被用户中止")

        archive_path = Path(archive).resolve()
        archive_str = str(archive_path)
        pw_provided = bool(password)
        pwd_flag = f"-p:{password}" if password else "-p:"

        smallest_member, header_encrypted = self._probe_archive_for_test(
            archive_path, timeout=timeout, task_id=task_id,
        )

        if header_encrypted:
            cmd = [str(self.exe_path), "l", pwd_flag, archive_str]
            result = self._run_command(cmd, timeout=timeout, task_id=task_id)
            if result.returncode == 0:
                return True
            try:
                self._classify_result(result, archive_str, pw_provided, password or "")
            except (WrongPasswordError, PasswordRequiredError):
                return False
            except ExtractionFailedError:
                # Bandizip 对加密文件头（如 RAR5 -hp）错密时可能返回 rc=2 + 0xa0000003
                combined = (result.stdout + result.stderr).lower()
                if "0xa0000003" in combined or "can't open archive" in combined:
                    return False
                raise
            return False

        base_cmd = [str(self.exe_path), "t", "-y", pwd_flag, archive_str]
        if smallest_member:
            targeted_cmd = base_cmd + [smallest_member]
            result = self._run_command(targeted_cmd, timeout=timeout, task_id=task_id)
            if result.returncode == 0:
                return True
            try:
                self._classify_result(result, archive_str, pw_provided, password or "")
            except (WrongPasswordError, PasswordRequiredError):
                return False
            except ExtractionFailedError:
                logger.debug(
                    f"Bandizip 定向成员验密返回非密码错误 ({smallest_member})，降级执行整包 t 验证"
                )
                fallback_res = self._run_command(base_cmd, timeout=timeout, task_id=task_id)
                if fallback_res.returncode == 0:
                    return True
                try:
                    self._classify_result(
                        fallback_res, archive_str, pw_provided, password or ""
                    )
                except (WrongPasswordError, PasswordRequiredError):
                    return False
                return False
            return False

        result = self._run_command(base_cmd, timeout=timeout, task_id=task_id)
        if result.returncode == 0:
            return True
        try:
            self._classify_result(result, archive_str, pw_provided, password or "")
        except (WrongPasswordError, PasswordRequiredError):
            return False
        return False

    def extract(
        self,
        archive: str | Path,
        dest: str | Path,
        password: Optional[str] = None,
        timeout: int = 300,
        task_id: Optional[str] = None,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> Path:
        archive_path = Path(archive).resolve()
        dest_path = Path(dest).resolve()
        archive_str = str(archive_path)
        dest_str = str(dest_path)
        os.makedirs(dest_str, exist_ok=True)

        # 铁律二：纯 List 传参
        cmd = [
            self.exe_path, "x",
            "-o:" + dest_str, "-y",
            archive_str,
        ]
        pw_provided = bool(password)
        if password:
            cmd.insert(-1, "-p:" + password)

        archive_size = self._safe_file_size(archive_path)
        result = self._run_command(
            cmd,
            timeout,
            task_id=task_id,
            on_progress=on_progress,
            monitor_dest=dest_path,
            archive_size=archive_size,
        )

        if result.returncode != 0:
            self._classify_result(result, archive_str, pw_provided, password or "")

        if on_progress is not None:
            try:
                on_progress(100)
            except Exception:
                pass

        logger.info(f"Bandizip 解压完成: {archive_str} → {dest_str}")
        return dest_path

    def repair_extract(
        self,
        archive: str | Path,
        dest: str | Path,
        password: Optional[str] = None,
        timeout: int = 600,
        task_id: Optional[str] = None,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> Path:
        archive_path = Path(archive).resolve()
        dest_path = Path(dest).resolve()
        archive_str = str(archive_path)
        dest_str = str(dest_path)
        os.makedirs(dest_str, exist_ok=True)

        cmd = [
            self.exe_path, "x",
            "-o:" + dest_str, "-y", "-aoa",
            archive_str,
        ]
        pw_provided = bool(password)
        if password:
            cmd.insert(-1, "-p:" + password)

        archive_size = self._safe_file_size(archive_path)
        result = self._run_command(
            cmd,
            timeout,
            task_id=task_id,
            on_progress=on_progress,
            monitor_dest=dest_path,
            archive_size=archive_size,
        )

        if result.returncode != 0:
            self._classify_result(result, archive_str, pw_provided, password or "")

        if on_progress is not None:
            try:
                on_progress(100)
            except Exception:
                pass

        logger.info(f"Bandizip repair 提取完成: {archive_str} → {dest_str}")
        return dest_path


class WinRAREngine(BaseEngine):
    """WinRAR 引擎 — RAR 格式优先调用同目录 UnRAR.exe 获取实时百分比，其余沿用 WinRAR.exe + 旁路采样。"""

    supports_fast_test: bool = True

    _RAR_LT_NAME_RE = re.compile(r"^\s*(?:Name|名称|名稱)\s*:\s*(.+?)\s*$", re.IGNORECASE)
    _RAR_LT_TYPE_RE = re.compile(r"^\s*(?:Type|类型|類型)\s*:\s*(.+?)\s*$", re.IGNORECASE)
    _RAR_LT_SIZE_RE = re.compile(r"^\s*(?:Size|大小)\s*:\s*(\d+)\s*$", re.IGNORECASE)
    _RAR_L_ROW_RE = re.compile(
        r"^\s*\*?\s*(\S+)\s+(\d+)\s+\d{2,4}[-/]\d{2}[-/]\d{2,4}\s+\d{2}:\d{2}(?::\d{2})?\s+(.+?)\s*$"
    )

    @classmethod
    def _parse_smallest_member(cls, stdout: str) -> Optional[str]:
        """从 UnRAR/WinRAR 的 lt 或 l 列表输出中解析体积最小的常规文件路径。"""
        if not stdout:
            return None

        # 1) 优先解析 lt 详细技术列表（兼容英文与简繁体中文）
        lt_candidates: list[tuple[int, str]] = []
        cur_name: Optional[str] = None
        cur_type: Optional[str] = None
        cur_size: Optional[int] = None

        def _flush() -> None:
            if cur_name and cur_size is not None:
                is_dir = (
                    (cur_type is not None and any(k in cur_type for k in ("dir", "目录", "目錄")))
                    or cur_name.endswith(("/", "\\"))
                )
                if not is_dir:
                    lt_candidates.append((cur_size, cur_name))

        for line in stdout.splitlines():
            m_name = cls._RAR_LT_NAME_RE.match(line)
            if m_name:
                _flush()
                cur_name = m_name.group(1).strip()
                cur_type = None
                cur_size = None
                continue
            if cur_name is None:
                continue
            m_type = cls._RAR_LT_TYPE_RE.match(line)
            if m_type:
                cur_type = m_type.group(1).strip().lower()
                continue
            m_size = cls._RAR_LT_SIZE_RE.match(line)
            if m_size:
                try:
                    cur_size = int(m_size.group(1))
                except ValueError:
                    pass
                continue
        _flush()

        if lt_candidates:
            lt_candidates.sort(key=lambda item: (item[0] == 0, item[0]))
            return lt_candidates[0][1]

        # 2) 回退解析 l 表格列表
        l_candidates: list[tuple[int, str]] = []
        for line in stdout.splitlines():
            m = cls._RAR_L_ROW_RE.match(line)
            if not m:
                continue
            attr, size_str, name = m.group(1), m.group(2), m.group(3).strip()
            if not name:
                continue
            if "d" in attr.lower() or name.endswith(("/", "\\")):
                continue
            try:
                size = int(size_str)
            except ValueError:
                continue
            l_candidates.append((size, name))

        if not l_candidates:
            return None
        l_candidates.sort(key=lambda item: (item[0] == 0, item[0]))
        return l_candidates[0][1]

    def _select_cli_exe(self, archive_path: Path) -> tuple[str, bool]:
        """对 .rar / RAR 魔数优先选择同目录 UnRAR.exe，其余走 WinRAR.exe。"""
        unrar_path = self._resolve_unrar_exe()
        use_unrar = (
            unrar_path is not None
            and (self._is_rar_archive(archive_path) or Path(self.exe_path).name.lower() == "unrar.exe")
        )
        if use_unrar:
            return str(unrar_path), True
        return self.exe_path, False

    @staticmethod
    def _build_cli_cmd(exe: str, use_unrar: bool, subcmd: str, extra: list[str]) -> list[str]:
        if use_unrar:
            return [exe, subcmd, *extra]
        return [exe, subcmd, "-ibck", "-inul", *extra]

    def _probe_archive_for_test(
        self,
        archive_path: Path,
        timeout: int = 60,
        task_id: Optional[str] = None,
    ) -> tuple[Optional[str], bool]:
        """
        探测压缩包是否为加密头或定位最小常规成员：
        - 退出码 11 或分类为密码异常 → (None, True)（加密文件头）；
        - 列表成功 → (smallest_member, False)；
        - 坏包/非密码错误 → 抛出 ExtractionFailedError。
        """
        cache_key = self._get_probe_cache_key(archive_path)
        cached = self._get_cached_probe(cache_key)
        if cached is not None:
            return cached

        archive_str = str(archive_path)
        exe, use_unrar = self._select_cli_exe(archive_path)
        cmd = self._build_cli_cmd(exe, use_unrar, "lt", ["-p-", archive_str])

        try:
            probe_timeout = min(timeout, 30) if use_unrar else min(timeout, 10)
            result = self._run_command(cmd, timeout=probe_timeout, task_id=task_id)
        except ExtractionFailedError as e:
            # GUI 版 WinRAR.exe 在非 RAR 文件上执行 lt 可能超时，平滑降级为整包测试
            if not use_unrar and e.returncode == -1:
                probed = (None, False)
                self._set_cached_probe(cache_key, probed)
                return probed
            raise

        if result.returncode == 11:
            # 标准加密 ZIP 中央目录未加密，绝不误标为加密文件头（否则 lb 验密会假阳性）
            if _is_encrypted_zip(archive_path):
                probed = (None, False)
            else:
                probed = (None, True)
            self._set_cached_probe(cache_key, probed)
            return probed

        if result.returncode not in (0, 1):
            if _is_encrypted_zip(archive_path) or self._is_embedded_polyglot(archive_path):
                logger.debug(
                    f"WinRAR 列表探测遇加密 ZIP/偏移图种 ({archive_path.name}, rc={result.returncode})，"
                    f"平滑降级为整包测试"
                )
                probed = (None, False)
                self._set_cached_probe(cache_key, probed)
                return probed
            try:
                self._classify_exit_code(
                    result, archive_str, pw_provided=False, password_used=""
                )
            except (PasswordRequiredError, WrongPasswordError):
                probed = (None, True)
                self._set_cached_probe(cache_key, probed)
                return probed

        smallest = self._parse_smallest_member(result.stdout)
        probed = (smallest, False)
        self._set_cached_probe(cache_key, probed)
        return probed

    def test_password(
        self,
        archive: str | Path,
        password: Optional[str] = None,
        timeout: int = 60,
        task_id: Optional[str] = None,
    ) -> bool:
        if task_id and self.is_aborted(task_id):
            raise ExtractionAbortedError(task_id, "任务已被用户中止")

        archive_path = Path(archive).resolve()
        archive_str = str(archive_path)
        pw_provided = bool(password)
        pwd_flag = f"-p{password}" if password else "-p-"

        smallest_member, header_encrypted = self._probe_archive_for_test(
            archive_path, timeout=timeout, task_id=task_id,
        )

        exe, use_unrar = self._select_cli_exe(archive_path)

        if header_encrypted:
            cmd = self._build_cli_cmd(exe, use_unrar, "lb", [pwd_flag, archive_str])
            result = self._run_command(cmd, timeout=timeout, task_id=task_id)
            if result.returncode in (0, 1):
                return True
            try:
                self._classify_exit_code(result, archive_str, pw_provided, password or "")
            except (WrongPasswordError, PasswordRequiredError):
                return False
            return False

        base_cmd = self._build_cli_cmd(exe, use_unrar, "t", ["-y", pwd_flag, archive_str])
        if smallest_member:
            targeted_cmd = base_cmd + [smallest_member]
            result = self._run_command(targeted_cmd, timeout=timeout, task_id=task_id)
            if result.returncode in (0, 1):
                return True
            try:
                self._classify_exit_code(result, archive_str, pw_provided, password or "")
            except (WrongPasswordError, PasswordRequiredError):
                return False
            except ExtractionFailedError:
                logger.debug(
                    f"WinRAR 定向成员验密返回非密码错误 ({smallest_member})，降级执行整包 t 验证"
                )
                fallback_res = self._run_command(base_cmd, timeout=timeout, task_id=task_id)
                if fallback_res.returncode in (0, 1):
                    return True
                try:
                    self._classify_exit_code(
                        fallback_res, archive_str, pw_provided, password or ""
                    )
                except (WrongPasswordError, PasswordRequiredError):
                    return False
                return False
            return False

        result = self._run_command(base_cmd, timeout=timeout, task_id=task_id)
        if result.returncode in (0, 1):
            return True
        try:
            self._classify_exit_code(result, archive_str, pw_provided, password or "")
        except (WrongPasswordError, PasswordRequiredError):
            return False
        return False

    @staticmethod
    def _is_rar_archive(archive_path: Path) -> bool:
        """检测是否为 RAR 格式（后缀 .rar 或前 7 字节匹配 Rar!\\x1a\\x07）。"""
        if archive_path.suffix.lower() == ".rar":
            return True
        try:
            with open(archive_path, "rb") as f:
                header = f.read(7)
            return header.startswith(b"Rar!\x1a\x07")
        except OSError:
            return False

    def _resolve_unrar_exe(self) -> Optional[Path]:
        """检查 WinRAR.exe 同目录下是否存在 UnRAR.exe（或自身即为 UnRAR.exe）。"""
        exe_p = Path(self.exe_path)
        if exe_p.name.lower() == "unrar.exe":
            return exe_p
        candidate = exe_p.parent / "UnRAR.exe"
        if candidate.is_file():
            return candidate
        return None

    def extract(
        self,
        archive: str | Path,
        dest: str | Path,
        password: Optional[str] = None,
        timeout: int = 300,
        task_id: Optional[str] = None,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> Path:
        archive_path = Path(archive).resolve()
        dest_path = Path(dest).resolve()
        archive_str = str(archive_path)
        dest_str = str(dest_path)
        os.makedirs(dest_str, exist_ok=True)

        pw_provided = bool(password)
        pwd_flag = f"-p{password}" if password else "-p-"

        unrar_path = self._resolve_unrar_exe()
        use_unrar = (
            unrar_path is not None
            and (self._is_rar_archive(archive_path) or Path(self.exe_path).name.lower() == "unrar.exe")
        )

        if use_unrar:
            # UnRAR.exe 不支持 -ibck，且不带 -inul 以输出实时百分比（CREATE_NO_WINDOW 已隐藏窗口）
            cmd = [
                str(unrar_path), "x", "-y", pwd_flag,
                archive_str, dest_str + os.sep,
            ]
        else:
            cmd = [
                self.exe_path, "x", "-ibck", "-inul", "-y", pwd_flag,
                archive_str, dest_str + os.sep,
            ]

        archive_size = self._safe_file_size(archive_path)
        result = self._run_command(
            cmd,
            timeout,
            task_id=task_id,
            on_progress=on_progress,
            monitor_dest=dest_path,
            archive_size=archive_size,
        )

        if result.returncode != 0:
            self._classify_exit_code(result, archive_str, pw_provided, password or "")

        if on_progress is not None:
            try:
                on_progress(100)
            except Exception:
                pass

        logger.info(f"WinRAR 解压完成: {archive_str} → {dest_str}")
        return dest_path

    @staticmethod
    def _classify_exit_code(
        result: subprocess.CompletedProcess,
        archive_str: str,
        pw_provided: bool,
        password_used: str = "",
    ) -> None:
        """
        WinRAR 错误码精确分类，优先级高于文本解析。
        退出码：0=成功 1=警告 2=致命 3=CRC 5=写入 6=打开
               7=命令行 8=内存 9=创建 10=无匹配 11=密码错误 12=读错 13=坏包 255=中断
        """
        code = result.returncode
        if code == 11:
            if pw_provided:
                raise WrongPasswordError(
                    archive_str, password_used,
                    f"WinRAR exit code {code}: 密码错误 | {result.stderr or ''}")
            else:
                raise PasswordRequiredError(
                    archive_str,
                    f"WinRAR exit code {code}: 压缩包已加密需要密码 | {result.stderr or ''}")
        if code == 1:
            logger.warning(
                "WinRAR 警告 (exit=%d): %s | %s",
                code, archive_str, (result.stderr or result.stdout or ""))
            return
        if code == 10 and _is_encrypted_zip(archive_str):
            if pw_provided:
                raise WrongPasswordError(
                    archive_str,
                    password_used,
                    f"WinRAR exit code 10: 加密 ZIP 归档密码错误 | {result.stderr or result.stdout or ''}",
                )
            raise PasswordRequiredError(
                archive_str,
                f"WinRAR exit code 10: 加密 ZIP 归档需要密码 | {result.stderr or result.stdout or ''}",
            )
        if code in (3, 5, 6, 7, 8, 9, 10, 12, 13, 255):
            raise ExtractionFailedError(
                archive_str, code,
                f"WinRAR exit code {code}: {result.stderr or result.stdout or ''}")

        BaseEngine._classify_result(result, archive_str, pw_provided, password_used)

    def supports_repair_for(self, archive: str | Path) -> bool:
        """WinRAR r 仅支持修复 RAR/ZIP 归档，对 .7z 等格式直接跳过，避免长时间无效盲扫。"""
        archive_path = Path(archive)
        if archive_path.suffix.lower() in {".rar", ".zip"}:
            return True
        try:
            from core.sandbox import detect_archive_type
            return detect_archive_type(archive_path) in {".rar", ".zip"}
        except Exception:
            return False

    def repair_extract(
        self,
        archive: str | Path,
        dest: str | Path,
        password: Optional[str] = None,
        timeout: int = 600,
        task_id: Optional[str] = None,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> Path:
        from core.sandbox import robust_rmtree

        archive_path = Path(archive).resolve()
        archive_str = str(archive_path)
        dest_str = str(Path(dest).resolve())

        if not self.supports_repair_for(archive_path):
            raise ExtractionFailedError(
                archive_str, -1, "WinRAR 仅支持修复 RAR/ZIP 归档"
            )

        # 在系统临时目录中执行修复，绝不污染用户源目录
        repaired_dir = Path(tempfile.mkdtemp(prefix="sb_repair_"))
        try:
            repair_cmd = [
                self.exe_path, "r", "-ibck", "-inul", "-y",
                archive_str, str(repaired_dir) + os.sep,
            ]
            if password:
                repair_cmd.insert(-2, f"-p{password}")
            else:
                repair_cmd.insert(-2, "-p-")

            # WinRAR r 无实时百分比输出：立即推送不确定态脉冲 (-1)，防止 UI 卡在 0%
            if on_progress is not None:
                try:
                    on_progress(-1)
                except Exception as cb_err:
                    logger.debug(f"修复进度不确定态回调异常: {cb_err}")

            logger.info(f"WinRAR 执行修复: {repair_cmd}")
            repair_res = self._run_command(repair_cmd, timeout=min(timeout, 300), task_id=task_id)

            repaired_archive = None
            for f in repaired_dir.iterdir():
                if f.is_file() and f.suffix.lower() in {".rar", ".zip"}:
                    repaired_archive = f
                    break

            if repaired_archive is None:
                if _is_header_encrypted_rar(archive_path):
                    rc = repair_res.returncode if repair_res is not None else 11
                    if password:
                        raise WrongPasswordError(
                            archive_str,
                            password,
                            f"WinRAR exit code 11 (repair rc={rc}): 加密文件头 RAR 密码错误",
                        )
                    raise PasswordRequiredError(
                        archive_str,
                        f"WinRAR exit code 11 (repair rc={rc}): 加密文件头 RAR 归档需要密码",
                    )
                if repair_res is not None and repair_res.returncode not in (0, 1):
                    # 若 repair 本身因密码需求/密码错退出（如返回 11 或加密 ZIP 返回 10），保留精准密码分类
                    self._classify_exit_code(
                        repair_res, archive_str, bool(password), password or ""
                    )
                logger.warning("WinRAR repair 未生成修复文件，直接失败（不重复执行原包 extract）")
                raise ExtractionFailedError(
                    archive_str,
                    repair_res.returncode if repair_res is not None else -1,
                    "WinRAR repair 未生成修复文件",
                )

            logger.info(f"WinRAR 修复文件: {repaired_archive}")
            return self.extract(
                str(repaired_archive),
                dest_str,
                password=password,
                timeout=timeout,
                task_id=task_id,
                on_progress=on_progress,
            )
        finally:
            robust_rmtree(repaired_dir)


class SevenZipEngine(BaseEngine):
    """7-Zip 引擎 (7z.exe / 7za.exe) — 原生支持 -bsp1 实时进度、-slt 快速验密与全格式零配置兜底。"""

    supports_fast_test: bool = True

    _7Z_TYPE_MAP: dict[str, str] = {
        ".zip": "zip",
        ".rar": "rar",
        ".7z": "7z",
        ".gz": "gzip",
        ".bz2": "bzip2",
        ".xz": "xz",
        ".tar": "tar",
    }

    _7Z_PASSWORD_KEYWORDS: tuple[str, ...] = (
        "wrong password",
        "can not open encrypted archive",
        "cannot open encrypted archive",
        "enter password",
        "data error in encrypted file",
        "crc failed in encrypted file",
        "encrypted archive",
        "encrypted file",
        "密码错误",
        "密码不正确",
    )

    @classmethod
    def _is_password_error(cls, combined: str) -> bool:
        """判断 7-Zip 输出文本是否命中密码错误/加密需要密码特征。"""
        if any(kw in combined for kw in cls._7Z_PASSWORD_KEYWORDS):
            return True
        if "headers error" in combined and ("encrypted" in combined or "password" in combined):
            return True
        return False

    @classmethod
    def _is_not_archive_error(cls, combined: str) -> bool:
        return (
            "is not archive" in combined
            or "cannot open the file as" in combined
            or "can not open the file as" in combined
            or "cannot open archive" in combined
            or "can not open archive" in combined
            or "headers error" in combined
        )

    @classmethod
    def _classify_7z_result(
        cls,
        result: subprocess.CompletedProcess,
        archive_str: str,
        pw_provided: bool,
        password_used: str = "",
    ) -> None:
        """
        7-Zip 退出码与输出分类：
        - 命中密码特征：传了密码抛 WrongPasswordError，未传密码抛 PasswordRequiredError；
        - 其余非零退出：抛 ExtractionFailedError。
        """
        combined = (result.stdout + "\n" + result.stderr).lower()
        if cls._is_password_error(combined):
            detail = result.stderr or result.stdout
            if pw_provided:
                raise WrongPasswordError(archive_str, password_used, detail)
            raise PasswordRequiredError(archive_str, detail)

        if result.returncode != 0:
            raise ExtractionFailedError(
                archive_str,
                result.returncode,
                result.stderr or result.stdout or f"7-Zip exit code {result.returncode}",
            )

    @classmethod
    def _detect_polyglot_type_flag(cls, archive_path: Path) -> Optional[str]:
        """当图种/偏移包被重命名为标准压缩包后缀导致 7z 默认 offset-0 打开失败时，探测显式 -t<type> 标志。"""
        try:
            from core.sandbox import detect_embedded_archive
            emb_ext, emb_offset = detect_embedded_archive(archive_path)
            if emb_ext is not None and emb_offset > 0:
                t_name = cls._7Z_TYPE_MAP.get(emb_ext.lower())
                if t_name:
                    return f"-t{t_name}"
        except Exception as e:
            logger.debug(f"7-Zip 图种偏移类型探测跳过 ({archive_path}): {e}")
        return None

    @staticmethod
    def _parse_slt_smallest_member(stdout: str) -> Optional[str]:
        """
        解析 `7z l -slt` 输出块中的 `Path = ...`、`Size = ...`、`Folder = -`，
        返回体积最小的非空常规文件路径；无有效候选时返回 None。
        """
        if not stdout:
            return None

        lines = stdout.splitlines()
        start_idx = 0
        for idx, line in enumerate(lines):
            if line.strip() == "----------":
                start_idx = idx + 1
                break

        candidates: list[tuple[int, str]] = []
        cur_path: Optional[str] = None
        cur_size: Optional[int] = None
        cur_folder: Optional[str] = None
        cur_attr: Optional[str] = None
        is_archive_header = False

        def _flush() -> None:
            if not cur_path or cur_size is None or is_archive_header:
                return
            if cur_folder == "+":
                return
            if cur_path.endswith(("/", "\\")):
                return
            if cur_folder is None and cur_attr:
                attr_head = cur_attr.split()[0].lower()
                if "d" in attr_head:
                    return
            if cur_size > 0:
                candidates.append((cur_size, cur_path))

        for line in lines[start_idx:]:
            stripped = line.strip()
            if not stripped:
                _flush()
                cur_path = None
                cur_size = None
                cur_folder = None
                cur_attr = None
                is_archive_header = False
                continue

            if " = " not in stripped:
                continue
            key, val = stripped.split(" = ", 1)
            key = key.strip()
            val = val.strip()

            if key == "Path":
                if cur_path is not None:
                    _flush()
                    cur_size = None
                    cur_folder = None
                    cur_attr = None
                    is_archive_header = False
                cur_path = val
            elif key == "Type":
                is_archive_header = True
            elif key == "Folder":
                cur_folder = val
            elif key == "Size":
                try:
                    cur_size = int(val)
                except ValueError:
                    cur_size = None
            elif key == "Attributes":
                cur_attr = val

        _flush()
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1]

    def _probe_archive_for_test(
        self,
        archive_path: Path,
        timeout: int = 60,
        task_id: Optional[str] = None,
    ) -> tuple[Optional[str], bool]:
        """
        无密探测压缩包结构以加速后续密码预检：
        - 报错命中加密特征 → 返回 (None, True)（加密文件头），后续用 `7z l -slt -p<pwd>` 毫秒级验密；
        - 列表成功 → 返回 (smallest_member, False)；
        - 坏包/非密码错误 → 抛出 ExtractionFailedError。
        """
        cache_key = self._get_probe_cache_key(archive_path)
        cached = self._get_cached_probe(cache_key)
        if cached is not None:
            return cached

        archive_str = str(archive_path)
        cmd = [self.exe_path, "l", "-slt", "-p", archive_str]
        result = self._run_command(cmd, timeout=min(timeout, 30), task_id=task_id)

        if result.returncode != 0:
            combined = (result.stdout + "\n" + result.stderr).lower()
            if not self._is_password_error(combined) and self._is_not_archive_error(combined):
                type_flag = self._detect_polyglot_type_flag(archive_path)
                if type_flag:
                    retry_cmd = [self.exe_path, "l", "-slt", type_flag, "-p", archive_str]
                    result = self._run_command(retry_cmd, timeout=min(timeout, 30), task_id=task_id)

        if result.returncode != 0:
            try:
                self._classify_7z_result(
                    result, archive_str, pw_provided=False, password_used=""
                )
            except (PasswordRequiredError, WrongPasswordError):
                probed = (None, True)
                self._set_cached_probe(cache_key, probed)
                return probed

        smallest = self._parse_slt_smallest_member(result.stdout)
        probed = (smallest, False)
        self._set_cached_probe(cache_key, probed)
        return probed

    def test_password(
        self,
        archive: str | Path,
        password: Optional[str] = None,
        timeout: int = 60,
        task_id: Optional[str] = None,
    ) -> bool:
        if task_id and self.is_aborted(task_id):
            raise ExtractionAbortedError(task_id, "任务已被用户中止")

        archive_path = Path(archive).resolve()
        archive_str = str(archive_path)
        pw_provided = bool(password)
        pwd_flag = f"-p{password or ''}"

        smallest_member, header_encrypted = self._probe_archive_for_test(
            archive_path, timeout=timeout, task_id=task_id,
        )

        def _run_with_polyglot_retry(cmd: list[str], insert_idx: int) -> subprocess.CompletedProcess:
            res = self._run_command(cmd, timeout=timeout, task_id=task_id)
            if res.returncode != 0:
                combined = (res.stdout + "\n" + res.stderr).lower()
                if not self._is_password_error(combined) and self._is_not_archive_error(combined):
                    type_flag = self._detect_polyglot_type_flag(archive_path)
                    if type_flag:
                        retry_cmd = list(cmd)
                        retry_cmd.insert(insert_idx, type_flag)
                        res = self._run_command(retry_cmd, timeout=timeout, task_id=task_id)
            return res

        if header_encrypted:
            cmd = [self.exe_path, "l", "-slt", pwd_flag, archive_str]
            result = _run_with_polyglot_retry(cmd, insert_idx=3)
            if result.returncode == 0:
                return True
            try:
                self._classify_7z_result(result, archive_str, pw_provided, password or "")
            except (WrongPasswordError, PasswordRequiredError):
                return False
            return False

        base_cmd = [self.exe_path, "t", "-y", pwd_flag, archive_str]
        if smallest_member:
            targeted_cmd = base_cmd + [smallest_member]
            result = _run_with_polyglot_retry(targeted_cmd, insert_idx=3)
            if result.returncode == 0:
                return True
            try:
                self._classify_7z_result(result, archive_str, pw_provided, password or "")
            except (WrongPasswordError, PasswordRequiredError):
                return False
            except ExtractionFailedError:
                logger.debug(
                    f"7-Zip 定向成员验密返回非密码错误 ({smallest_member})，降级执行整包 t 验证"
                )
                fallback_res = _run_with_polyglot_retry(base_cmd, insert_idx=3)
                if fallback_res.returncode == 0:
                    return True
                try:
                    self._classify_7z_result(
                        fallback_res, archive_str, pw_provided, password or ""
                    )
                except (WrongPasswordError, PasswordRequiredError):
                    return False
                return False
            return False

        result = _run_with_polyglot_retry(base_cmd, insert_idx=3)
        if result.returncode == 0:
            return True
        try:
            self._classify_7z_result(result, archive_str, pw_provided, password or "")
        except (WrongPasswordError, PasswordRequiredError):
            return False
        return False

    def extract(
        self,
        archive: str | Path,
        dest: str | Path,
        password: Optional[str] = None,
        timeout: int = 300,
        task_id: Optional[str] = None,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> Path:
        if task_id and self.is_aborted(task_id):
            raise ExtractionAbortedError(task_id, "任务已被用户中止")

        archive_path = Path(archive).resolve()
        dest_path = Path(dest).resolve()
        archive_str = str(archive_path)
        dest_str = str(dest_path)
        os.makedirs(dest_str, exist_ok=True)

        pw_provided = bool(password)
        pwd_flag = f"-p{password}" if password else "-p"
        cmd = [
            self.exe_path,
            "x",
            "-y",
            "-bsp1",
            pwd_flag,
            f"-o{dest_str}",
            archive_str,
        ]

        archive_size = self._safe_file_size(archive_path)
        result = self._run_command(
            cmd,
            timeout,
            task_id=task_id,
            on_progress=on_progress,
            monitor_dest=dest_path,
            archive_size=archive_size,
        )

        # 图种/偏移复合包兼容：若重命名后的扩展名导致 7z offset-0 打开失败，自动附加 -t<type> 重试
        if result.returncode != 0:
            combined = (result.stdout + "\n" + result.stderr).lower()
            if not self._is_password_error(combined) and self._is_not_archive_error(combined):
                type_flag = self._detect_polyglot_type_flag(archive_path)
                if type_flag:
                    retry_cmd = [
                        self.exe_path,
                        "x",
                        "-y",
                        "-bsp1",
                        type_flag,
                        pwd_flag,
                        f"-o{dest_str}",
                        archive_str,
                    ]
                    logger.info(
                        f"7-Zip 检测到图种偏移包，附加 {type_flag} 重试提取: {archive_path.name}"
                    )
                    result = self._run_command(
                        retry_cmd,
                        timeout,
                        task_id=task_id,
                        on_progress=on_progress,
                        monitor_dest=dest_path,
                        archive_size=archive_size,
                    )

        if result.returncode == 1:
            combined = (result.stdout + "\n" + result.stderr).lower()
            has_output = dest_path.exists() and any(dest_path.iterdir())
            if has_output and not self._is_password_error(combined):
                logger.warning(
                    "7-Zip 非致命警告 (exit=1，已产出有效文件): %s | %s",
                    archive_str,
                    (result.stderr or result.stdout or "")[:200],
                )
            else:
                self._classify_7z_result(result, archive_str, pw_provided, password or "")
        elif result.returncode != 0:
            self._classify_7z_result(result, archive_str, pw_provided, password or "")

        if on_progress is not None:
            try:
                on_progress(100)
            except Exception:
                pass

        logger.info(f"7-Zip 解压完成: {archive_str} → {dest_str}")
        return dest_path

    def supports_repair_for(self, archive: str | Path) -> bool:
        """7-Zip x 原生具备容错能力，无需在链 ③ 重复执行相同命令。"""
        return False

    def repair_extract(
        self,
        archive: str | Path,
        dest: str | Path,
        password: Optional[str] = None,
        timeout: int = 300,
        task_id: Optional[str] = None,
        on_progress: Optional[Callable[[int], None]] = None,
    ) -> Path:
        """7-Zip x 原生支持偏移/多态图种和容错提取，直接委托调用 extract。"""
        return self.extract(
            archive,
            dest,
            password=password,
            timeout=timeout,
            task_id=task_id,
            on_progress=on_progress,
        )


# ---------- 引擎工厂 ----------

def _find_bandizip_cli() -> str | None:
    """
    定位 Bandizip CLI 内核 (bz.exe)。
    递归搜索 Bandizip 安装目录，绝不在 subprocess 中调用 GUI 版。
    若找不到 bz.exe，尝试 PATH 环境变量。
    """
    for root in BANDIZIP_INSTALL_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            if "bz.exe" in filenames:
                bz_path = os.path.join(dirpath, "bz.exe")
                logger.info(f"发现 Bandizip CLI（递归搜索）: {bz_path}")
                return bz_path

    found = shutil.which("bz.exe")
    if found:
        logger.info(f"发现 Bandizip CLI（PATH）: {found}")
        return found

    logger.debug("未找到 bz.exe，Bandizip CLI 内核缺失。")
    return None


def _find_winrar_cli() -> Optional[str]:
    """定位系统安装的 WinRAR.exe 或 UnRAR.exe。"""
    for p in WINRAR_PATHS:
        if os.path.isfile(p):
            logger.info(f"发现 WinRAR 引擎: {p}")
            return p
    for p in UNRAR_PATHS:
        if os.path.isfile(p):
            logger.info(f"发现 UnRAR 引擎: {p}")
            return p
    return None


def _find_bundled_sevenzip() -> Optional[str]:
    """
    查找随应用内置的 7-Zip 二进制（兼容 PyInstaller frozen 与源码模式）：
    优先查找同目录携带 7z.dll 的 7z.exe（全格式支持 RAR/RAR5/ISO/ZSTD），其次查找 7za.exe。
    """
    candidate_dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidate_dirs.append(Path(meipass) / "assets" / "bin")
        candidate_dirs.append(Path(sys.executable).parent / "assets" / "bin")
    else:
        candidate_dirs.append(Path(__file__).resolve().parent.parent / "assets" / "bin")

    # 第一优先级：完整版 7z.exe + 7z.dll
    for bin_dir in candidate_dirs:
        exe_7z = bin_dir / "7z.exe"
        dll_7z = bin_dir / "7z.dll"
        if exe_7z.is_file() and dll_7z.is_file():
            resolved = str(exe_7z.resolve())
            logger.info(f"发现内置 7-Zip 完整版 (7z.exe + 7z.dll): {resolved}")
            return resolved

    # 第二优先级：独立版 7za.exe
    for bin_dir in candidate_dirs:
        exe_7za = bin_dir / "7za.exe"
        if exe_7za.is_file():
            resolved = str(exe_7za.resolve())
            logger.info(f"发现内置 7-Zip 独立版 (7za.exe): {resolved}")
            return resolved

    # 兜底：若仅存在 7z.exe（例如 7z.dll 位于系统 PATH）
    for bin_dir in candidate_dirs:
        exe_7z = bin_dir / "7z.exe"
        if exe_7z.is_file():
            resolved = str(exe_7z.resolve())
            logger.info(f"发现内置 7-Zip (7z.exe): {resolved}")
            return resolved

    return None


def _find_sevenzip_cli() -> Optional[str]:
    """
    自动发现 7-Zip CLI：
    优先使用全格式 7z.exe（内置 assets/bin/7z.exe 或系统安装路径 / PATH），
    若无全格式版本再降级至 7za.exe（内置或 PATH）。
    """
    bundled = _find_bundled_sevenzip()
    if bundled and Path(bundled).name.lower() != "7za.exe":
        return bundled

    for p in SEVENZIP_PATHS:
        if os.path.isfile(p):
            logger.info(f"发现系统 7-Zip 引擎: {p}")
            return p

    found_7z = shutil.which("7z") or shutil.which("7z.exe")
    if found_7z:
        logger.info(f"发现 7-Zip CLI（PATH）: {found_7z}")
        return found_7z

    if bundled:
        return bundled

    found_7za = shutil.which("7za") or shutil.which("7za.exe")
    if found_7za:
        logger.info(f"发现 7za CLI（PATH）: {found_7za}")
        return found_7za

    logger.debug("未找到 7-Zip CLI 内核。")
    return None


def discover_engine(config=None) -> tuple[Optional[EngineType], Optional[str]]:
    """
    柔性三引擎自动发现：
    - 支持 default_engine 偏好 ("bandizip" | "winrar" | "7zip") 及手动路径配置；
    - 优先级：用户偏好引擎（手动路径 → 自动发现）→ 其余两个引擎（手动路径 → 自动发现）；
    - 内置 assets/bin/7z.exe / 7za.exe 作为零配置最终兜底内核；
    - 全部缺失时返回 (None, None)，绝不抛异常。
    """
    if config is None:
        from data.config_manager import ConfigManager
        cfg = ConfigManager()
    else:
        cfg = config

    default_engine = str(cfg.get("default_engine", "bandizip") or "bandizip").strip().lower()
    bz_path = str(cfg.get("bandizip_path", "") or "").strip()
    wr_path = str(cfg.get("winrar_path", "") or "").strip()
    sz_path = str(cfg.get("sevenzip_path", "") or "").strip()

    if default_engine in ("7zip", "sevenzip", "7z"):
        order = ("7zip", "bandizip", "winrar")
    elif default_engine == "winrar":
        order = ("winrar", "bandizip", "7zip")
    else:
        order = ("bandizip", "winrar", "7zip")

    engine_specs = {
        "bandizip": (EngineType.BANDIZIP, bz_path, _find_bandizip_cli, "Bandizip"),
        "winrar": (EngineType.WINRAR, wr_path, _find_winrar_cli, "WinRAR"),
        "7zip": (EngineType.SEVENZIP, sz_path, _find_sevenzip_cli, "7-Zip"),
    }

    for key in order:
        etype, manual_path, finder, label = engine_specs[key]
        if manual_path:
            if key == "bandizip":
                resolved_bz = resolve_bandizip_cli(manual_path)
                if resolved_bz:
                    logger.info(f"使用用户配置的 {label} CLI 路径: {resolved_bz}")
                    return etype, resolved_bz
                logger.warning(
                    f"用户配置的 Bandizip 路径不可用或非命令行程序 (需要 bz.exe，已忽略并降级): {manual_path}"
                )
            elif os.path.isfile(manual_path):
                logger.info(f"使用用户配置的 {label} 路径: {manual_path}")
                return etype, manual_path
        found = finder()
        if found:
            return etype, found

    logger.warning("未找到任何解压引擎，请前往全局设置手动选择引擎路径")
    return None, None


def create_engine(exe_path: Optional[str], engine_type: Optional[EngineType]) -> Optional[BaseEngine]:
    """创建引擎实例。若 exe_path 或 engine_type 为 None，返回 None。"""
    if exe_path is None or engine_type is None:
        return None
    if engine_type == EngineType.BANDIZIP:
        return BandizipEngine(exe_path)
    if engine_type == EngineType.SEVENZIP:
        return SevenZipEngine(exe_path)
    return WinRAREngine(exe_path)


def get_engine_display_name(engine_type: Optional[EngineType]) -> str:
    """获取引擎显示名称。None 时返回提示文本。"""
    if engine_type is None:
        return "未配置 - 请前往全局设置手动选择引擎路径"
    if engine_type == EngineType.BANDIZIP:
        return "Bandizip (bz.exe)"
    if engine_type == EngineType.SEVENZIP:
        return "7-Zip"
    return "WinRAR"