"""
沙箱管理器 — 铁律四：隔离沙箱防覆盖
每个解压任务在专属沙箱中执行，伪装包必须 shutil.copy() 实体重命名。
阶段 4.7：新增 deliver_to_target() 搬运交付 + cleanup_sandbox() 回收。
"""
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

# 分卷正则
PART_PATTERN = re.compile(r"^(.*)\.part(\d+)\.rar$", re.IGNORECASE)


def _ensure_unique_path(target: Path) -> Path:
    """若 target 已存在，自动追加 _1, _2 ... 序号直到找到空闲路径。"""
    if not target.exists():
        return target
    parent = target.parent
    stem = target.stem
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}"
        if not candidate.exists():
            return candidate
        counter += 1


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
        is_known = ext in KNOWN_ARCHIVE_EXTS
        if PART_PATTERN.match(src.name):
            is_known = True

        if is_known:
            logger.debug(f"合法压缩包，无需重命名: {src}")
            return src

        dest = sandbox / (src.stem + ".zip")
        logger.info(f"检测到伪装包 ({ext})，shutil.copy → {dest}")
        shutil.copy(src, dest)
        return dest

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

        cfg = ConfigManager()
        output_mode = cfg.get("output_mode", "source_directory")
        custom_path = cfg.get("custom_output_path", "")

        target_root = Path(custom_path) if (output_mode == "custom" and custom_path) else src.parent
        target_base = target_root / src.stem

        final_target = _ensure_unique_path(target_base)
        final_target.mkdir(parents=True, exist_ok=True)

        count = 0
        for item in output_dir.iterdir():
            dest = final_target / item.name
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
        【原子搬运阶段】— 简单物理搬运，零破坏原则。

        —— 仅基于 output 根目录的 os.listdir() 做表面判断 ——
        1. 清理系统隐藏文件
        2. output 根目录恰好只有 1 个目录 → Data Body 已就位，等待 deliver_to_target
        3. output 根目录有 0 个或多于 1 个条目 → 保持现状

        【绝对红线】
        - 绝不递归扫描 Data Body 内部（禁止 rglob / riterdir）
        - 绝不对目录内部做任何拆解或移动（禁止拍平）
        - Data Body 是什么层级就保持什么层级，作为原子单位交付
        """
        if not output_dir.exists() or not output_dir.is_dir():
            return

        for entry in list(output_dir.iterdir()):
            if entry.name in SandboxManager._SYSTEM_FILES and entry.is_file():
                try:
                    entry.unlink()
                    logger.debug(f"提权：已删除系统文件 '{entry.name}'")
                except OSError:
                    pass

        entries = [
            e for e in output_dir.iterdir()
            if e.name not in SandboxManager._SYSTEM_FILES
        ]

        if len(entries) != 1:
            logger.info(
                f"提权：output 根目录有 {len(entries)} 个条目，"
                f"保持现状（不做搬运，保护多文件结构完整性）"
            )
            return

        sole = entries[0]
        if not sole.is_dir():
            logger.info(
                f"提权：output 根目录唯一条目为文件 '{sole.name}'，"
                f"保持现状（不搬运单文件）"
            )
            return

        logger.info(
            f"提权：Data Body 确认为 '{sole.name}'，"
            f"目录层级保持完整，等待 deliver_to_target 原子搬运"
        )

        for entry in list(output_dir.iterdir()):
            if (entry.is_dir()
                    and entry.name not in SandboxManager._SYSTEM_FILES
                    and entry != sole):
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