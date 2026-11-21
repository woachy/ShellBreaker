"""
沙箱管理器 — 铁律四：隔离沙箱防覆盖
每个解压任务在专属沙箱中执行，伪装包必须 shutil.copy() 实体重命名。
阶段 4.7：新增 deliver_to_target() 搬运交付 + cleanup_sandbox() 回收。
"""
import os
import json
import shutil
import logging
import re
import stat
import sys
import tempfile
import threading
import time
import ctypes
import zipfile
from ctypes import wintypes
from pathlib import Path
from typing import Optional

from core.exceptions import SandboxError

logger = logging.getLogger("ShellBreaker.Sandbox")

# 沙箱根目录
if getattr(sys, "frozen", False):
    SANDBOX_ROOT = Path(sys.executable).parent / "logs" / "sandbox_temp"
else:
    SANDBOX_ROOT = Path(__file__).resolve().parent.parent / "logs" / "sandbox_temp"

# 同盘隐藏沙箱容器目录名与 Windows 隐藏属性常量
SANDBOX_CONTAINER_NAME = ".shellbreaker_sandbox"
FILE_ATTRIBUTE_HIDDEN = 0x02

# 伪装重命名事务日志路径及模块级并发锁
RENAME_JOURNAL_PATH = SANDBOX_ROOT.parent / "rename_journal.json"
_JOURNAL_LOCK = threading.RLock()

# 已知的压缩包后缀（无需重命名的合法后缀）
KNOWN_ARCHIVE_EXTS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
    ".arj", ".lzh", ".cab", ".iso", ".uue", ".z", ".001",
}

# 受保护的终点程序白名单：无论内部结构为何，绝对禁止当作压缩包拆解
PROTECTED_ENDPOINT_EXTS = frozenset({".apk", ".exe", ".msi"})

# 常见伪装后缀白名单
POTENTIAL_DISGUISE_EXTS = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp",
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv",
    ".pdf", ".bin", ".dat", ".iso", ".bak",
})

# 说明与元数据配件文件后缀（允许附带在大文件旁，不破坏孤儿形态）
ACCESSORY_EXTS = frozenset({".txt", ".url", ".nfo", ".html", ".htm", ".md"})

# 分卷正则
PART_PATTERN = re.compile(r"^(.*)\.part(\d+)\.rar$", re.IGNORECASE)


class SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", wintypes.WORD),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


FO_DELETE = 0x0003
# 标志位：移至回收站 (0x0040) | 不弹确认框 (0x0010) | 静默无进度条 (0x0004) | 不弹系统报错窗 (0x0400)
FOF_RECYCLE_SILENT = 0x0040 | 0x0010 | 0x0004 | 0x0400


def send_to_recycle_bin(file_path: Path | str) -> bool:
    """将文件静默安全移至 Windows 回收站（要求双 Null 结尾宽字符缓冲区）。"""
    p = Path(file_path).resolve()
    if not p.is_file():
        return False
    buffer = f"{str(p)}\0\0"
    op = SHFILEOPSTRUCTW()
    op.hwnd = None
    op.wFunc = FO_DELETE
    op.pFrom = buffer
    op.pTo = None
    op.fFlags = FOF_RECYCLE_SILENT
    try:
        ret = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
        return ret == 0 and not op.fAnyOperationsAborted
    except Exception as e:
        logger.warning(f"SHFileOperationW 移入回收站失败 ({p}): {e}")
        return False


def is_primary_part(filename: str) -> bool:
    """判断文件是否为主/首卷分卷（part1/part01 或 .001 / .7z.001 / .zip.001）。"""
    m_rar = re.search(r"\.part(\d+)\.rar$", filename, re.IGNORECASE)
    if m_rar:
        return int(m_rar.group(1)) == 1
    m_split = re.search(r"\.(?:7z|zip)\.(\d+)$", filename, re.IGNORECASE)
    if m_split:
        return int(m_split.group(1)) == 1
    m_gen = re.search(r"\.(\d{3,})$", filename, re.IGNORECASE)
    if m_gen:
        return int(m_gen.group(1)) == 1
    return False


def is_secondary_volume(filename: str) -> bool:
    """判断文件是否为非首卷分卷（part02+ 或 .002+ 或经典 .r00+ / .z01+）。"""
    m_part = re.search(r"\.part(\d+)\.rar$", filename, re.IGNORECASE)
    if m_part:
        return int(m_part.group(1)) > 1
    m_num = re.search(r"\.(?:7z|zip)\.(\d+)$", filename, re.IGNORECASE)
    if m_num:
        return int(m_num.group(1)) > 1
    m_gen = re.search(r"\.(\d{3,})$", filename, re.IGNORECASE)
    if m_gen:
        return int(m_gen.group(1)) > 1
    m_classic = re.search(r"\.[rz]\d{2,}$", filename, re.IGNORECASE)
    if m_classic:
        return True
    return False


def get_part_group(src_path: str | Path) -> list[Path]:
    """
    查找与给定分卷文件同组的所有分卷文件（支持 .partN.rar 任意补零、.7z.001 / .zip.001、
    通用数字分卷 .001 / .002，以及经典分卷 .rar/.r00 / .zip/.z01）。
    """
    src = Path(src_path).resolve()
    if not src.parent.exists():
        return [src]

    # 1. 匹配 .partN.rar 格式（兼容任意补零位数）
    m_rar = re.match(r"^(.*)\.part\d+\.rar$", src.name, re.IGNORECASE)
    if m_rar:
        base = re.escape(m_rar.group(1))
        pattern = re.compile(rf"^{base}\.part\d+\.rar$", re.IGNORECASE)
        try:
            parts = sorted([p for p in src.parent.iterdir() if p.is_file() and pattern.match(p.name)])
            return parts if parts else [src]
        except OSError:
            return [src]

    # 2. 匹配 .7z.001 / .zip.001 分卷
    m_split = re.match(r"^(.*)\.(7z|zip)\.\d+$", src.name, re.IGNORECASE)
    if m_split:
        base = re.escape(m_split.group(1))
        ext = re.escape(m_split.group(2))
        pattern = re.compile(rf"^{base}\.{ext}\.\d+$", re.IGNORECASE)
        try:
            parts = sorted([p for p in src.parent.iterdir() if p.is_file() and pattern.match(p.name)])
            return parts if parts else [src]
        except OSError:
            return [src]

    # 3. 匹配通用纯数字分卷 .001, .002 等
    m_gen = re.match(r"^(.*)\.(\d{3,})$", src.name, re.IGNORECASE)
    if m_gen:
        base = re.escape(m_gen.group(1))
        pattern = re.compile(rf"^{base}\.\d+$", re.IGNORECASE)
        try:
            parts = sorted([p for p in src.parent.iterdir() if p.is_file() and pattern.match(p.name)])
            return parts if parts else [src]
        except OSError:
            return [src]

    # 4. 匹配经典 RAR 分卷 (.rar, .r00, .r01...)
    m_classic_rar = re.match(r"^(.*)\.(?:rar|r\d{2,})$", src.name, re.IGNORECASE)
    if m_classic_rar:
        base = re.escape(m_classic_rar.group(1))
        pattern = re.compile(rf"^{base}\.(?:rar|r\d+)$", re.IGNORECASE)
        try:
            parts = sorted([p for p in src.parent.iterdir() if p.is_file() and pattern.match(p.name)])
            if len(parts) > 1:
                return parts
        except OSError:
            pass

    # 5. 匹配经典 ZIP 分卷 (.zip, .z01, .z02...)
    m_classic_zip = re.match(r"^(.*)\.(?:zip|z\d{2,})$", src.name, re.IGNORECASE)
    if m_classic_zip:
        base = re.escape(m_classic_zip.group(1))
        pattern = re.compile(rf"^{base}\.(?:zip|z\d+)$", re.IGNORECASE)
        try:
            parts = sorted([p for p in src.parent.iterdir() if p.is_file() and pattern.match(p.name)])
            if len(parts) > 1:
                return parts
        except OSError:
            pass

    return [src]


_EMBEDDED_CHUNK_SIZE = 1024 * 1024  # 1MB
_EMBEDDED_OVERLAP = 32
_EMBEDDED_TAIL_SCAN_BYTES = 8 * 1024 * 1024  # 8MB
_EMBEDDED_ARCHIVE_MAGICS: tuple[tuple[bytes, str], ...] = (
    (b"Rar!\x1a\x07\x00", ".rar"),      # RAR4
    (b"Rar!\x1a\x07\x01\x00", ".rar"),  # RAR5
    (b"\x37\x7a\xbc\xaf\x27\x1c", ".7z"),  # 7z
)


def _is_media_carrier_header(header: bytes) -> bool:
    """判断文件头部是否属于常见图种/媒体载体格式（JPEG/PNG/GIF/BMP/WebP/MP4/MOV）。"""
    if header.startswith(b"\xff\xd8\xff"):
        return True  # JPEG
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return True  # PNG
    if header.startswith((b"GIF87a", b"GIF89a")):
        return True  # GIF
    if header.startswith(b"BM"):
        return True  # BMP
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return True  # WebP
    if len(header) >= 8 and header[4:8] == b"ftyp":
        return True  # MP4 / MOV
    return False


def _verify_zip_structure(path: Path, file_obj, file_size: int) -> bool:
    """对内嵌 PK\\x03\\x04 执行二次防误报校验（检查末尾 EOCD PK\\x05\\x06 或 zipfile.is_zipfile）。"""
    try:
        tail_len = min(file_size, 128 * 1024)
        if tail_len >= 4:
            cur_pos = file_obj.tell()
            file_obj.seek(file_size - tail_len)
            tail_data = file_obj.read(tail_len)
            file_obj.seek(cur_pos)
            if b"PK\x05\x06" in tail_data:
                return True
    except OSError:
        pass
    try:
        return zipfile.is_zipfile(path)
    except Exception:
        return False


def _scan_range_for_embedded_magic(
    path: Path,
    file_obj,
    file_size: int,
    start_pos: int,
    end_pos: int,
    zip_verified: Optional[bool],
) -> tuple[Optional[str], int, Optional[bool]]:
    """流式分块扫描指定区间 [start_pos, end_pos)，保留 32 字节跨块重叠以防止魔数截断。"""
    if start_pos >= end_pos:
        return None, -1, zip_verified

    file_obj.seek(start_pos)
    prev_tail = b""
    cursor = start_pos

    while cursor < end_pos:
        to_read = min(_EMBEDDED_CHUNK_SIZE, end_pos - cursor)
        chunk = file_obj.read(to_read)
        if not chunk:
            break

        buf = prev_tail + chunk
        buf_start = cursor - len(prev_tail)
        search_start = 1 if buf_start == 0 else 0

        candidates: list[tuple[int, str]] = []
        for magic, ext in _EMBEDDED_ARCHIVE_MAGICS:
            pos = buf.find(magic, search_start)
            if pos != -1:
                candidates.append((pos, ext))

        if zip_verified is not False:
            zip_pos = buf.find(b"PK\x03\x04", search_start)
            if zip_pos != -1:
                if zip_verified is None:
                    zip_verified = _verify_zip_structure(path, file_obj, file_size)
                if zip_verified:
                    candidates.append((zip_pos, ".zip"))

        if candidates:
            best_pos, best_ext = min(candidates, key=lambda item: item[0])
            return best_ext, buf_start + best_pos, zip_verified

        cursor += len(chunk)
        prev_tail = buf[-_EMBEDDED_OVERLAP:] if len(buf) >= _EMBEDDED_OVERLAP else buf

    return None, -1, zip_verified


def detect_embedded_archive(
    file_path: Path | str,
    max_scan_bytes: int = 64 * 1024 * 1024,
) -> tuple[Optional[str], int]:
    """
    图种 / 复合伪装魔数偏移嗅探：
    返回 (archive_ext, offset)，未命中或受保护终点文件返回 (None, -1)。
    """
    path = Path(file_path)
    if not path.is_file():
        return None, -1

    ext = path.suffix.lower()
    if ext in PROTECTED_ENDPOINT_EXTS:
        return None, -1

    try:
        file_size = path.stat().st_size
        with open(path, "rb") as f:
            header = f.read(16)
            if not header:
                return None, -1

            # 1. 检测偏移 0 的传统压缩包魔数
            if header.startswith(b"\x37\x7a\xbc\xaf\x27\x1c"):
                return ".7z", 0
            if header.startswith(b"Rar!\x1a\x07"):
                return ".rar", 0
            if header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
                return ".zip", 0
            if header.startswith(b"\x1f\x8b"):
                return ".gz", 0
            if header.startswith(b"BZh"):
                return ".bz2", 0
            if header.startswith(b"\xfd7zXZ\x00"):
                return ".xz", 0

            # 2. 若属于常见媒体载体头，执行流式分块扫描查找偏移 > 0 的内嵌压缩包魔数
            is_media = _is_media_carrier_header(header)
            if is_media:
                zip_verified: Optional[bool] = None
                scan_limit = max(0, int(max_scan_bytes))
                head_end = min(file_size, scan_limit)
                found_ext, found_offset, zip_verified = _scan_range_for_embedded_magic(
                    path, f, file_size, 0, head_end, zip_verified
                )
                if found_ext is not None:
                    return found_ext, found_offset

                if file_size > scan_limit:
                    tail_start = max(
                        max(0, scan_limit - _EMBEDDED_OVERLAP),
                        file_size - _EMBEDDED_TAIL_SCAN_BYTES,
                    )
                    found_ext, found_offset, _ = _scan_range_for_embedded_magic(
                        path, f, file_size, tail_start, file_size, zip_verified
                    )
                    if found_ext is not None:
                        return found_ext, found_offset

                return None, -1
    except OSError as e:
        logger.warning(f"无法读取文件头部魔数 ({path}): {e}")
        return None, -1

    # 3. 非媒体小文件保留 zipfile.is_zipfile() 兜底检测
    if file_size <= max(0, int(max_scan_bytes)):
        try:
            if zipfile.is_zipfile(path):
                return ".zip", 0
        except Exception:
            pass

    return None, -1


def detect_archive_type(file_path: Path | str) -> Optional[str]:
    """
    通过魔数与图种偏移嗅探检测真实压缩包类型。
    若非压缩包（如真正的图片、视频）返回 None。
    保护白名单 (.apk, .exe, .msi) 无论内部结构为何，绝不识别为压缩包。
    """
    ext, _ = detect_embedded_archive(file_path)
    return ext


def check_volume_completeness(primary_archive: Path | str) -> tuple[bool, list[str]]:
    """
    分卷连续性预检：扫描同目录下同前缀分卷是否连续。
    覆盖 4 种常见分卷族：
    1. .partN.rar / .part01.rar
    2. .7z.001 / .zip.001 / .rar.001 / 通用 .001
    3. 经典 .rar + .r00, .r01, ...
    4. 经典 .zip + .z01, .z02, ...
    返回 (is_complete, missing_labels)。
    """
    path = Path(primary_archive)
    if not path.is_file():
        return True, []

    parent = path.parent
    try:
        siblings = [entry for entry in parent.iterdir() if entry.is_file()]
    except OSError:
        return True, []

    name = path.name

    # 1. .partN.rar
    m_part = re.match(r"^(.*)\.part(\d+)\.rar$", name, re.IGNORECASE)
    if m_part:
        base = re.escape(m_part.group(1))
        pattern = re.compile(rf"^{base}\.part(\d+)\.rar$", re.IGNORECASE)
        nums: set[int] = set()
        digits_list: list[str] = []
        for entry in siblings:
            m = pattern.match(entry.name)
            if m:
                raw_digits = m.group(1)
                nums.add(int(raw_digits))
                digits_list.append(raw_digits)
        if not nums or max(nums) <= 1:
            return True, []
        padded_widths = [len(d) for d in digits_list if len(d) > 1 and d.startswith("0")]
        width = max(padded_widths) if padded_widths else 0
        missing = [
            f"part{k:0{width}d}" if width > 0 else f"part{k}"
            for k in range(1, max(nums) + 1)
            if k not in nums
        ]
        return (len(missing) == 0, missing)

    # 2. .7z.001 / .zip.001 / .rar.001 / 通用 .001
    m_split = re.match(r"^(.*)\.(\d{3,})$", name, re.IGNORECASE)
    if m_split:
        base = re.escape(m_split.group(1))
        pattern = re.compile(rf"^{base}\.(\d{{3,}})$", re.IGNORECASE)
        nums = set()
        digits_list = []
        for entry in siblings:
            m = pattern.match(entry.name)
            if m:
                raw_digits = m.group(1)
                nums.add(int(raw_digits))
                digits_list.append(raw_digits)
        if not nums:
            return True, []
        padded_widths = [len(d) for d in digits_list if len(d) >= 3 and d.startswith("0")]
        width = max(padded_widths) if padded_widths else len(m_split.group(2))
        missing = [
            f"{k:0{width}d}"
            for k in range(1, max(nums) + 1)
            if k not in nums
        ]
        return (len(missing) == 0, missing)

    # 3. 经典 .rar + .r00, .r01, ...
    m_classic_rar = re.match(r"^(.*)\.(?:rar|r\d{2})$", name, re.IGNORECASE)
    if m_classic_rar:
        base = re.escape(m_classic_rar.group(1))
        pattern = re.compile(rf"^{base}\.r(\d{{2}})$", re.IGNORECASE)
        r_nums: set[int] = set()
        for entry in siblings:
            m = pattern.match(entry.name)
            if m:
                r_nums.add(int(m.group(1)))
        if not r_nums:
            return True, []
        missing = [
            f"r{k:02d}"
            for k in range(0, max(r_nums) + 1)
            if k not in r_nums
        ]
        return (len(missing) == 0, missing)

    # 4. 经典 .zip + .z01, .z02, ...
    m_classic_zip = re.match(r"^(.*)\.(?:zip|z\d{2})$", name, re.IGNORECASE)
    if m_classic_zip:
        base = re.escape(m_classic_zip.group(1))
        pattern = re.compile(rf"^{base}\.z(\d{{2}})$", re.IGNORECASE)
        z_nums: set[int] = set()
        for entry in siblings:
            m = pattern.match(entry.name)
            if m:
                z_nums.add(int(m.group(1)))
        if not z_nums:
            return True, []
        missing = [
            f"z{k:02d}"
            for k in range(1, max(z_nums) + 1)
            if k not in z_nums
        ]
        return (len(missing) == 0, missing)

    return True, []


def ensure_unique_path(target: Path) -> Path:
    """若 target 已存在，自动追加 _1, _2 ... 序号直到找到空闲路径（保留文件扩展名）。"""
    if not target.exists():
        return target
    parent = target.parent
    counter = 1
    if target.is_dir() or not target.suffix:
        base_name = target.name
        while True:
            candidate = parent / f"{base_name}_{counter}"
            if not candidate.exists():
                return candidate
            counter += 1
    else:
        stem = target.stem
        suffix = target.suffix
        while True:
            candidate = parent / f"{stem}_{counter}{suffix}"
            if not candidate.exists():
                return candidate
            counter += 1


_ensure_unique_path = ensure_unique_path


def robust_rmtree(path: Path | str, max_retries: int = 4) -> bool:
    """
    健壮的目录递归删除工具：
    - 处理目录不存在直接返回 True；
    - 兼容 Python 3.10 ~ 3.12+ 的 onexc / onerror 回调，清除只读属性后重试删除；
    - 外层指数退避重试（0.1s, 0.2s, 0.4s, 0.8s），应对 Windows 强杀子进程后文件句柄延迟释放；
    - 最终目录不存在返回 True，全部重试失败记录 warning 并返回 False。
    """
    target = Path(path)
    if not target.exists():
        return True

    last_error: Exception | None = None

    def _handle_remove_error(func, target_path, exc_or_info):
        nonlocal last_error
        try:
            os.chmod(target_path, stat.S_IWRITE)
            func(target_path)
        except OSError as exc:
            last_error = exc

    rmtree_kwargs = (
        {"onexc": _handle_remove_error}
        if sys.version_info >= (3, 12)
        else {"onerror": _handle_remove_error}
    )

    attempts = max(0, int(max_retries)) + 1
    for attempt in range(attempts):
        if not target.exists():
            return True
        try:
            shutil.rmtree(target, **rmtree_kwargs)
        except OSError as e:
            last_error = e

        if not target.exists():
            return True

        if attempt < attempts - 1:
            delay = 0.1 * (2 ** attempt)
            logger.debug(
                f"robust_rmtree 第 {attempt + 1} 次删除未完成 ({target})，"
                f"{delay:.1f}s 后重试..."
            )
            time.sleep(delay)

    if not target.exists():
        return True

    logger.warning(
        f"robust_rmtree 经过 {max_retries} 次重试仍无法删除目录 ({target})"
        + (f": {last_error}" if last_error else "")
    )
    return False


def _cleanup_empty_sandbox_container(container: Path) -> None:
    """若给定目录为 .shellbreaker_sandbox 且已空，则自动删除以避免在用户磁盘留下空隐藏文件夹。"""
    try:
        if (
            container.name == SANDBOX_CONTAINER_NAME
            and container.exists()
            and container.is_dir()
            and not any(container.iterdir())
        ):
            container.rmdir()
            logger.debug(f"已清理空同盘沙箱容器: {container}")
    except OSError as e:
        logger.debug(f"清理空同盘沙箱容器跳过 ({container}): {e}")


def get_sandbox_dir_for_target(target_dir: Path | str, task_id: str) -> Path:
    """
    同盘隐藏沙箱动态路由：
    - 解析 target_dir，若其为文件或不存在则取其父目录；
    - 在同盘父目录下创建隐藏容器 `.shellbreaker_sandbox`（盘符根目录则直接在根下创建）；
    - Windows 下设置 FILE_ATTRIBUTE_HIDDEN (0x02)，失败仅记录 debug 日志不阻断；
    - 创建并验证 `container / task_id` 可写；
    - 遇到 OSError / PermissionError 时记录 warning 日志并自动回退到 `SANDBOX_ROOT / task_id`。
    """
    safe_task_id = SandboxManager._sanitize_name(str(task_id)) or "unnamed"
    container: Optional[Path] = None
    try:
        target_path = Path(target_dir).resolve()
        if target_path.is_file() or not target_path.exists():
            target_path = target_path.parent

        if target_path.parent != target_path:
            container = target_path.parent / SANDBOX_CONTAINER_NAME
        else:
            container = target_path / SANDBOX_CONTAINER_NAME

        container.mkdir(parents=True, exist_ok=True)

        if os.name == "nt":
            try:
                ret = ctypes.windll.kernel32.SetFileAttributesW(
                    str(container), FILE_ATTRIBUTE_HIDDEN
                )
                if ret == 0:
                    logger.debug(f"设置同盘沙箱隐藏属性返回 0 ({container})")
            except Exception as e:
                logger.debug(f"设置同盘沙箱隐藏属性失败 ({container}): {e}")

        sandbox = container / safe_task_id
        sandbox.mkdir(parents=True, exist_ok=True)

        # 探针写入验证可写性，及时捕获只读目录或 ACL 写权限拒绝
        probe = sandbox / f".sb_write_probe_{os.getpid()}_{threading.get_ident()}"
        probe.write_bytes(b"")
        probe.unlink(missing_ok=True)
        return sandbox
    except OSError as e:
        if container is not None:
            _cleanup_empty_sandbox_container(container)
        logger.warning(
            f"同盘隐藏沙箱不可用 ({target_dir})，自动回退至默认沙箱目录 ({SANDBOX_ROOT}): {e}"
        )
        fallback = SANDBOX_ROOT / safe_task_id
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


# ---------- 伪装重命名事务日志与崩溃自愈 ----------

def _normalize_path_key(path_value: Path | str) -> str:
    return os.path.normcase(os.path.normpath(str(path_value)))


def _is_pid_alive(pid: int) -> bool:
    """判断指定 PID 的进程是否在系统中存活（Windows 优先使用 OpenProcess + GetExitCodeProcess）。"""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False

    if os.name == "nt":
        try:
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                err = kernel32.GetLastError()
                # ERROR_ACCESS_DENIED (5) 表示目标进程存在但当前权限无法打开
                return err == 5
            try:
                exit_code = wintypes.DWORD()
                ok = kernel32.GetExitCodeProcess(
                    wintypes.HANDLE(handle), ctypes.byref(exit_code)
                )
                return bool(ok) and exit_code.value == 259
            finally:
                kernel32.CloseHandle(wintypes.HANDLE(handle))
        except Exception as e:
            logger.debug(f"Windows PID 存活检查异常 (pid={pid})，回退 os.kill: {e}")

    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _load_journal_entries(jpath: Path) -> list[dict]:
    """容错读取事务日志条目列表；文件不存在或损坏时返回空列表。"""
    if not jpath.exists():
        return []
    try:
        raw = jpath.read_text(encoding="utf-8")
        if not raw.strip():
            return []
        data = json.loads(raw)
        if not isinstance(data, list):
            logger.warning(f"重命名事务日志格式异常（非列表），已忽略: {jpath}")
            return []
        return [item for item in data if isinstance(item, dict)]
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning(f"读取重命名事务日志失败（已容错跳过）({jpath}): {e}")
        return []


def _save_journal_entries(jpath: Path, entries: list[dict]) -> None:
    """使用同目录临时文件 + os.replace 原子写盘（UTF-8，ensure_ascii=False）。"""
    jpath.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(jpath.parent),
            prefix=f".{jpath.name}.",
            suffix=".tmp",
            delete=False,
        ) as tf:
            tmp_name = tf.name
            json.dump(entries, tf, ensure_ascii=False, indent=2)
            tf.flush()
            os.fsync(tf.fileno())
        os.replace(tmp_name, jpath)
        tmp_name = None
    finally:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


def record_rename_journal(
    task_id: str,
    original_path: Path | str,
    temp_path: Path | str,
    journal_path: Optional[Path | str] = None,
) -> None:
    """
    记录一条伪装包原位重命名事务日志（原子写盘），用于在进程异常崩溃后还原原文件名。
    """
    jpath = Path(journal_path) if journal_path is not None else RENAME_JOURNAL_PATH
    temp_key = _normalize_path_key(temp_path)
    task_id_str = str(task_id)

    with _JOURNAL_LOCK:
        entries = _load_journal_entries(jpath)
        entries = [
            e for e in entries
            if not (
                str(e.get("task_id", "")) == task_id_str
                and _normalize_path_key(e.get("temp_path", "")) == temp_key
            )
        ]
        entries.append(
            {
                "task_id": task_id_str,
                "original_path": str(original_path),
                "temp_path": str(temp_path),
                "pid": os.getpid(),
                "timestamp": time.time(),
            }
        )
        _save_journal_entries(jpath, entries)


def remove_rename_journal(
    task_id: str,
    temp_path: Optional[Path | str] = None,
    journal_path: Optional[Path | str] = None,
) -> None:
    """
    从事务日志中移除匹配 task_id（若提供 temp_path 则同时匹配 temp_path）的记录并原子写回。
    """
    jpath = Path(journal_path) if journal_path is not None else RENAME_JOURNAL_PATH
    task_id_str = str(task_id)
    temp_key = _normalize_path_key(temp_path) if temp_path is not None else None

    with _JOURNAL_LOCK:
        if not jpath.exists():
            return
        entries = _load_journal_entries(jpath)
        remaining: list[dict] = []
        for entry in entries:
            same_task = str(entry.get("task_id", "")) == task_id_str
            if not same_task:
                remaining.append(entry)
                continue
            if temp_key is not None and _normalize_path_key(entry.get("temp_path", "")) != temp_key:
                remaining.append(entry)
        _save_journal_entries(jpath, remaining)


def recover_interrupted_renames(
    journal_path: Optional[Path | str] = None,
    force: bool = False,
) -> int:
    """
    扫描伪装重命名事务日志并自愈异常中断遗留的临时文件名。
    - 日志不存在或损坏时容错返回 0；
    - 当 force=False 且记录属于其他仍在存活的进程时，跳过并保留该记录；
    - 若 temp_path 存在且 original_path 不存在，尝试 os.replace(temp_path, original_path) 还原；
    - 陈旧条目（temp_path 不存在或 original_path 已恢复）自动清理；
    - 原子写回剩余条目，返回成功恢复的文件数。
    """
    jpath = Path(journal_path) if journal_path is not None else RENAME_JOURNAL_PATH

    with _JOURNAL_LOCK:
        if not jpath.exists():
            return 0
        entries = _load_journal_entries(jpath)
        if not entries:
            return 0

        recovered = 0
        remaining: list[dict] = []
        current_pid = os.getpid()

        for entry in entries:
            orig_raw = entry.get("original_path")
            temp_raw = entry.get("temp_path")
            if not orig_raw or not temp_raw:
                continue

            pid = entry.get("pid")
            if (
                not force
                and isinstance(pid, int)
                and not isinstance(pid, bool)
                and pid != current_pid
                and _is_pid_alive(pid)
            ):
                remaining.append(entry)
                continue

            temp_p = Path(temp_raw)
            orig_p = Path(orig_raw)
            task_id = entry.get("task_id", "")

            if temp_p.exists() and not orig_p.exists():
                try:
                    os.replace(str(temp_p), str(orig_p))
                    recovered += 1
                    logger.info(
                        f"伪装重命名事务自愈成功 [{task_id}]: {temp_p} → {orig_p}"
                    )
                except OSError as e:
                    logger.warning(
                        f"伪装重命名事务自愈失败 [{task_id}] ({temp_p} → {orig_p}): {e}"
                    )
                    remaining.append(entry)
            else:
                logger.debug(
                    f"清理陈旧重命名事务条目 [{task_id}]: temp_exists={temp_p.exists()}, orig_exists={orig_p.exists()}"
                )

        _save_journal_entries(jpath, remaining)
        return recovered


class SandboxManager:
    """管理解压任务的沙箱目录及文件准备。"""

    def __init__(self):
        SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._active_sandboxes: dict[str, Path] = {}

    # ---------- 沙箱创建 ----------

    def create_sandbox(
        self,
        task_id: str | Path,
        target_dir: Optional[Path | str] = None,
    ) -> Path:
        """
        创建任务专属沙箱目录：
        - 传入 target_dir 时，通过 get_sandbox_dir_for_target() 动态路由至目标同盘隐藏沙箱；
        - 未传 target_dir（默认 None）时，保持原有在 SANDBOX_ROOT 下创建的兼容行为。
        """
        raw_id = str(task_id)
        is_path_like = isinstance(task_id, Path) or (
            os.sep in raw_id
            or "/" in raw_id
            or "\\" in raw_id
            or (len(raw_id) >= 2 and raw_id[1] == ":")
        )
        base_name = Path(task_id).stem if is_path_like else raw_id
        safe_name = self._sanitize_name(base_name) or "unnamed"

        with self._lock:
            if target_dir is not None:
                sandbox = get_sandbox_dir_for_target(target_dir, safe_name)
                track_key = raw_id
                if track_key in self._active_sandboxes and self._active_sandboxes[track_key] != sandbox:
                    key_counter = 1
                    while f"{track_key}#{key_counter}" in self._active_sandboxes:
                        key_counter += 1
                    track_key = f"{track_key}#{key_counter}"
                self._active_sandboxes[track_key] = sandbox
                logger.info(f"沙箱已创建（同盘路由）: {sandbox}")
                return sandbox

            SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
            sandbox = SANDBOX_ROOT / safe_name
            counter = 1
            while sandbox.exists():
                sandbox = SANDBOX_ROOT / f"{safe_name}_{counter}"
                counter += 1

            sandbox.mkdir(parents=True, exist_ok=False)
            track_key = raw_id
            if track_key in self._active_sandboxes:
                key_counter = 1
                while f"{track_key}#{key_counter}" in self._active_sandboxes:
                    key_counter += 1
                track_key = f"{track_key}#{key_counter}"
            self._active_sandboxes[track_key] = sandbox
            logger.info(f"沙箱已创建: {sandbox}")
            return sandbox

    @staticmethod
    def _sanitize_name(name: str) -> str:
        illegal = r'[<>:"/\\|?*]'
        cleaned = re.sub(illegal, "_", name)
        return cleaned.strip().rstrip(".")

    # ---------- 文件准备（铁律四）----------

    def prepare_file(self, src_path: str | Path, sandbox: Path) -> Path:
        src = Path(src_path).resolve()
        if not src.exists():
            raise SandboxError(f"源文件不存在: {src}")

        ext = src.suffix.lower()

        # 终点白名单保护：.apk (及 .exe, .msi) 绝不作为压缩包拆解
        if ext in PROTECTED_ENDPOINT_EXTS:
            raise SandboxError(f"受保护的终点程序文件 ({ext})，禁止作为压缩包解压: {src.name}")

        is_known = ext in KNOWN_ARCHIVE_EXTS
        if PART_PATTERN.match(src.name) or is_primary_part(src.name) or is_secondary_volume(src.name):
            is_known = True

        if is_known:
            logger.debug(f"合法压缩包，无需重命名: {src}")
            return src

        # 伪装包处理：若魔数识别出具体格式则用对应后缀，
        # 若魔数未识别（例如各类图种），直接默认使用 .zip 后缀，保持第一层容错解压能力
        real_ext = detect_archive_type(src) or ".zip"

        dest = sandbox / (src.stem + real_ext)
        logger.info(f"检测到伪装包 ({ext} -> {real_ext})，准备进入沙箱 → {dest}")
        try:
            os.link(src, dest)
            logger.debug(f"通过硬链接准备文件: {dest}")
        except (OSError, AttributeError):
            shutil.copy(src, dest)
            logger.debug(f"通过复制准备文件: {dest}")
        return dest

    # ---------- 暂存目录解套与清理 ----------

    @staticmethod
    def unwrap_temp_dirs(root_dir: Path) -> None:
        """
        清理或解套所有残留的以 _tmp_ 开头的临时目录。
        自底向上（按路径层级由深到浅）遍历：
        - 若为空目录，直接删除；
        - 若非空，将其所有子项原子上移至其 parent 目录（重名追加序号），再删除该空目录。
        """
        if not root_dir.exists() or not root_dir.is_dir():
            return

        tmp_dirs = []
        for dirpath, dirnames, _ in os.walk(str(root_dir)):
            for d in dirnames:
                if d.startswith("_tmp_"):
                    tmp_dirs.append(Path(dirpath) / d)

        # 按层级深度从深到浅排序，优先处理最内层的 _tmp_
        tmp_dirs.sort(key=lambda p: len(p.parts), reverse=True)

        for tmp_dir in tmp_dirs:
            if not tmp_dir.exists() or not tmp_dir.is_dir():
                continue
            parent = tmp_dir.parent
            # 移动所有子项到 parent
            for item in list(tmp_dir.iterdir()):
                dest = ensure_unique_path(parent / item.name)
                try:
                    shutil.move(str(item), str(dest))
                    logger.debug(f"解套 _tmp_ 残留: {item} → {dest}")
                except Exception as e:
                    logger.warning(f"解套 _tmp_ 移动失败 ({item} → {dest}): {e}")
            # 删除空的 _tmp_ 目录
            try:
                shutil.rmtree(str(tmp_dir), ignore_errors=True)
                logger.info(f"提权：已清理暂存目录 '{tmp_dir.name}'")
            except OSError as e:
                logger.warning(f"清理暂存目录失败 ({tmp_dir}): {e}")

    # ---------- 搬运交付（阶段 4.7）----------

    @staticmethod
    def deliver_to_target(src_path: str | Path, sandbox_dir: Path | str) -> str:
        """
        将沙箱 output 目录中的所有内容搬运到用户配置的目标路径。
        同分区优先使用 os.replace() 实现零拷贝原子重命名；跨分区或特殊情况回退 shutil.move。
        防覆盖：同名文件/文件夹自动追加 _1, _2 序号。
        返回最终路径绝对字符串。
        """
        from data.config_manager import ConfigManager

        src = Path(src_path).resolve()
        sandbox_path = Path(sandbox_dir)
        output_dir = sandbox_path / "output"
        if not output_dir.exists() or not any(output_dir.iterdir()):
            raise SandboxError(f"沙箱 output 目录为空: {output_dir}")

        # 确保送入 deliver_to_target 的只有干净的业务内容（最后防线）
        SandboxManager.unwrap_temp_dirs(output_dir)

        cfg = ConfigManager()
        output_mode = cfg.get("output_mode", "source_directory")
        custom_path = cfg.get("custom_output_path", "")

        target_root = Path(custom_path) if (output_mode == "custom" and custom_path) else src.parent
        target_base = target_root / src.stem

        final_target = ensure_unique_path(target_base)
        final_target.mkdir(parents=True, exist_ok=True)

        count = 0
        for item in list(output_dir.iterdir()):
            if item.name in SandboxManager._SYSTEM_FILES:
                continue
            dest = ensure_unique_path(final_target / item.name)
            try:
                os.replace(str(item), str(dest))
                logger.debug(f"零拷贝原子搬运: {item.name} → {dest}")
            except OSError as e:
                logger.debug(f"os.replace 回退至 shutil.move ({item.name} → {dest}): {e}")
                shutil.move(str(item), str(dest))
                logger.debug(f"搬运: {item.name} → {dest}")
            count += 1

        logger.info(f"[交付成功] 文件已移动至: {final_target}（共 {count} 项）")
        return str(final_target.resolve())

    # ---------- 容器提权 ----------

    _SYSTEM_FILES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini", ".localized", "._.DS_Store"})

    @staticmethod
    def promote_content(output_dir: Path) -> None:
        """
        【容器提权阶段】
        1. 清理或解套所有残留的 _tmp_ 目录，消除中间包装壳。
        2. 清理系统隐藏文件 (.DS_Store 等)。
        3. 回收空壳目录。
        """
        if not output_dir.exists() or not output_dir.is_dir():
            return

        # 第一步：解套并清理所有残留的 _tmp_ 目录
        SandboxManager.unwrap_temp_dirs(output_dir)

        # 第二步：清理系统文件
        for entry in list(output_dir.iterdir()):
            if entry.name in SandboxManager._SYSTEM_FILES and entry.is_file():
                try:
                    entry.unlink()
                    logger.debug(f"提权：已删除系统文件 '{entry.name}'")
                except OSError:
                    pass

        # 第三步：回收空壳目录
        for entry in list(output_dir.iterdir()):
            if (entry.is_dir()
                    and entry.name not in SandboxManager._SYSTEM_FILES):
                try:
                    if not any(entry.iterdir()):
                        entry.rmdir()
                        logger.info(f"提权：已回收空壳目录 '{entry.name}'")
                except OSError as e:
                    logger.warning(f"提权：回收空壳失败 ({entry.name}): {e}")

    def cleanup_sandbox(self, sandbox_dir: Path | str):
        """
        删除整个沙箱目录（仅在搬运成功后或异常清理时调用）。
        若其父目录为 .shellbreaker_sandbox 且此时已空，自动清理该空容器目录。
        """
        sandbox_path = Path(sandbox_dir).resolve()
        if sandbox_path.exists():
            if robust_rmtree(sandbox_path):
                logger.info(f"沙箱已回收: {sandbox_path}")
            else:
                logger.warning(f"沙箱回收失败（存在残留）: {sandbox_path}")

        _cleanup_empty_sandbox_container(sandbox_path.parent)

        with self._lock:
            to_remove = [
                k for k, v in self._active_sandboxes.items()
                if Path(v).resolve() == sandbox_path
            ]
            for k in to_remove:
                self._active_sandboxes.pop(k, None)

    # ---------- 一键清空所有沙箱 ----------

    def clean_all(self) -> int:
        with self._lock:
            tracked = list(self._active_sandboxes.items())
            self._active_sandboxes.clear()

        removed = 0
        cleaned_paths: set[Path] = set()

        for _, sandbox_path in tracked:
            resolved = Path(sandbox_path).resolve()
            if resolved in cleaned_paths:
                continue
            cleaned_paths.add(resolved)
            try:
                if resolved.exists():
                    if robust_rmtree(resolved):
                        removed += 1
                        logger.info(f"沙箱已清理: {resolved}")
                    else:
                        logger.warning(f"清理沙箱失败 ({resolved})")
            except OSError as e:
                logger.warning(f"清理沙箱失败 ({resolved}): {e}")
            _cleanup_empty_sandbox_container(resolved.parent)

        if SANDBOX_ROOT.exists():
            for child in SANDBOX_ROOT.iterdir():
                resolved_child = child.resolve()
                if resolved_child in cleaned_paths:
                    continue
                try:
                    if child.is_dir():
                        if robust_rmtree(child):
                            removed += 1
                            logger.info(f"沙箱已清理: {child}")
                        else:
                            logger.warning(f"清理沙箱失败 ({child})")
                except OSError as e:
                    logger.warning(f"清理沙箱失败 ({child}): {e}")
        return removed

    # ---------- 分卷分析与回收站 ----------

    @staticmethod
    def is_primary_part(filename: str) -> bool:
        return is_primary_part(filename)

    @staticmethod
    def is_secondary_volume(filename: str) -> bool:
        return is_secondary_volume(filename)

    @staticmethod
    def get_part_group(src_path: str | Path) -> list[Path]:
        return get_part_group(src_path)

    @staticmethod
    def check_volume_completeness(primary_archive: Path | str) -> tuple[bool, list[str]]:
        return check_volume_completeness(primary_archive)

    @staticmethod
    def send_to_recycle_bin(file_path: Path | str) -> bool:
        return send_to_recycle_bin(file_path)