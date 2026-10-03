"""
沙箱管理器 — 铁律四：隔离沙箱防覆盖
每个解压任务在专属沙箱中执行，伪装包必须 shutil.copy() 实体重命名。
阶段 4.7：新增 deliver_to_target() 搬运交付 + cleanup_sandbox() 回收。
"""
import os
import shutil
import logging
import re
import sys
from pathlib import Path
from typing import Optional

from core.exceptions import SandboxError

logger = logging.getLogger("ShellBreaker.Sandbox")

# 沙箱根目录
if getattr(sys, "frozen", False):
    SANDBOX_ROOT = Path(sys.executable).parent / "logs" / "sandbox_temp"
else:
    SANDBOX_ROOT = Path(__file__).resolve().parent.parent / "logs" / "sandbox_temp"

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


def detect_archive_type(file_path: str | Path) -> Optional[str]:
    """
    通过读取文件头部魔数（前 16 字节）检测真实压缩包类型。
    若非压缩包（如真正的图片、视频）返回 None。
    保护白名单 (.apk, .exe, .msi) 无论内部结构为何，绝不识别为压缩包。

    魔数规范：
    - ZIP: PK\\x03\\x04 / PK\\x05\\x06 / PK\\x07\\x08 或 zipfile.is_zipfile 兜底
    - RAR: Rar!\\x1a\\x07 (RAR 4.x / 5.x)
    - 7Z:  7z\\xbc\\xaf'\\x1c (0x37 0x7A 0xBC 0xAF 0x27 0x1C)
    """
    path = Path(file_path)
    if not path.is_file():
        return None

    ext = path.suffix.lower()
    if ext in PROTECTED_ENDPOINT_EXTS:
        return None

    try:
        with open(path, "rb") as f:
            header = f.read(16)
    except OSError as e:
        logger.warning(f"无法读取文件头部魔数 ({path}): {e}")
        return None

    if len(header) < 4:
        return None

    # 1. 7-Zip: 6 字节 -> 37 7A BC AF 27 1C
    if header.startswith(b"\x37\x7a\xbc\xaf\x27\x1c"):
        return ".7z"

    # 2. RAR: 6 字节 -> 52 61 72 21 1A 07 (RAR 4.x / 5.x 均匹配)
    if header.startswith(b"Rar!\x1a\x07"):
        return ".rar"

    # 3. ZIP: 4 字节 -> PK\x03\x04, PK\x05\x06, PK\x07\x08
    if header.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        return ".zip"

    # 4. Gzip
    if header.startswith(b"\x1f\x8b"):
        return ".gz"

    # 5. Bzip2
    if header.startswith(b"BZh"):
        return ".bz2"

    # 6. XZ
    if header.startswith(b"\xfd7zXZ\x00"):
        return ".xz"

    # 7. zipfile.is_zipfile 兜底验证 (尾部 central dir 或自解压结构)
    try:
        import zipfile
        if zipfile.is_zipfile(path):
            return ".zip"
    except Exception:
        pass

    return None


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


class SandboxManager:
    """管理解压任务的沙箱目录及文件准备。"""

    def __init__(self):
        SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)
        self._active_sandboxes: list[Path] = []

    # ---------- 沙箱创建 ----------

    def create_sandbox(self, src_path: str | Path) -> Path:
        src = Path(src_path).resolve()
        stem = src.stem

        safe_stem = self._sanitize_name(stem)
        if not safe_stem:
            safe_stem = "unnamed"

        sandbox = SANDBOX_ROOT / safe_stem
        counter = 1
        while sandbox.exists():
            sandbox = SANDBOX_ROOT / f"{safe_stem}_{counter}"
            counter += 1

        sandbox.mkdir(parents=True, exist_ok=False)
        self._active_sandboxes.append(sandbox)
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
        if PART_PATTERN.match(src.name):
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
    def deliver_to_target(src_path: str | Path, sandbox_dir: Path) -> str:
        """
        将沙箱 output 目录中的所有内容搬运到用户配置的目标路径。
        防覆盖：同名文件/文件夹自动追加 _1, _2 序号。
        返回最终路径绝对字符串。
        """
        from data.config_manager import ConfigManager

        src = Path(src_path).resolve()
        output_dir = sandbox_dir / "output"
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
        for item in output_dir.iterdir():
            if item.name in SandboxManager._SYSTEM_FILES:
                continue
            dest = ensure_unique_path(final_target / item.name)
            shutil.move(str(item), str(dest))
            count += 1
            logger.debug(f"搬运: {item.name} → {dest}")

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

    @staticmethod
    def cleanup_sandbox(sandbox_dir: Path):
        """删除整个沙箱目录（仅在搬运成功后调用）。"""
        if sandbox_dir.exists():
            shutil.rmtree(sandbox_dir)
            logger.info(f"沙箱已回收: {sandbox_dir}")

    # ---------- 一键清空所有沙箱 ----------

    def clean_all(self):
        if not SANDBOX_ROOT.exists():
            return
        removed = 0
        for child in SANDBOX_ROOT.iterdir():
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                    removed += 1
                    logger.info(f"沙箱已清理: {child}")
            except OSError as e:
                logger.warning(f"清理沙箱失败 ({child}): {e}")
        self._active_sandboxes.clear()
        return removed

    # ---------- 分卷分析 ----------

    @staticmethod
    def is_primary_part(filename: str) -> bool:
        m = PART_PATTERN.match(filename)
        if not m:
            return False
        part_num = int(m.group(2))
        return part_num == 1

    @staticmethod
    def get_part_group(src_path: str | Path) -> list[Path]:
        src = Path(src_path).resolve()
        m = PART_PATTERN.match(src.name)
        if not m:
            return [src]

        base = m.group(1)
        parent = src.parent
        parts = []
        n = 1
        while True:
            candidate = parent / f"{base}.part{n}.rar"
            if candidate.exists():
                parts.append(candidate)
                n += 1
            else:
                break
        return parts if parts else [src]