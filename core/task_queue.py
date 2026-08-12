"""
多线程任务调度器 — 阶段 4.5：修复密码 Fallback 劫持 + QTableWidget 信号 + 全局设置联动
"""
import os
import logging
import uuid
import shutil
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Optional

from PySide6.QtCore import QObject, Signal

from core.exceptions import (
    PasswordRequiredError,
    WrongPasswordError,
    ZipBombDetectedError,
    ExtractionFailedError,
    SandboxError,
)
from core.sandbox import SandboxManager
from core.engine import (
    EngineType,
    BaseEngine,
    discover_engine,
    create_engine,
    get_engine_display_name,
)
from data.password_manager import PasswordManager
from data.config_manager import ConfigManager

logger = logging.getLogger("ShellBreaker.TaskQueue")

MAX_NEST_DEPTH = 50
NESTED_ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}
BLIND_EXTRACT_EXTS = {".zip", ".rar", ".7z", ".001", ".tar", ".gz", ".bz2", ".xz"}

# 递归扫描目标后缀：标准压缩包 + 常见伪装后缀（白名单）
SCAN_TARGET_EXTS = frozenset({
    # 标准压缩包
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2",
    # 伪装后缀
    ".mp4", ".jpg", ".png", ".pdf",
})


class TaskQueue(QObject):
    """
    多线程解压任务调度器。
    - 降维试错链：原文件盲解 → 改名 .zip 再试 → repair_extract
    - 密码问题绝不进入 Fallback（战役一修复）
    - 密码自学习：JSON 库全失败后弹窗，正确密码自动入库
    - 通过 task_status_changed 信号驱动 QTableWidget
    """

    task_status_changed = Signal(str, str, str, str, str)  # (task_id, filename, status, engine, remark)
    task_progress = Signal(str, str)                        # (task_id, status_text)
    password_needed = Signal(str, str)                      # (task_id, archive_basename)
    password_wrong = Signal(str)                            # (task_id) — 密码错误，通知 UI 重新输入

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self._config = ConfigManager()
        self._executor: Optional[ThreadPoolExecutor] = None
        self._futures: dict[Future, str] = {}
        self._running = False

        self._sandbox = SandboxManager()
        self._pwd_mgr = PasswordManager()
        self._engine: Optional[BaseEngine] = None
        self._engine_type: Optional[EngineType] = None
        self._engine_name: str = ""

        self._password_events: dict[str, threading.Event] = {}
        self._supplied_passwords: dict[str, Optional[str]] = {}
        self._lock = threading.Lock()

    # ---------- 生命周期 ----------

    def start(self):
        if self._running:
            return
        max_w = int(self._config.get("max_workers", 2))
        self._executor = ThreadPoolExecutor(max_workers=max_w)
        self._running = True
        # 预热引擎
        self._ensure_engine()
        logger.info(f"任务调度器已启动，最大并发: {max_w}，引擎: {self._engine_name}")

    def shutdown(self, wait: bool = True):
        self._running = False
        with self._lock:
            for evt in self._password_events.values():
                evt.set()
        if self._executor:
            self._executor.shutdown(wait=wait, cancel_futures=not wait)
            self._executor = None
        logger.info("任务调度器已关闭")

    def reload_config(self):
        """从 config.json 重新读取并发数并重启线程池。"""
        max_w = int(self._config.get("max_workers", 2))
        self._engine = None  # 强制下次重新发现引擎
        self._engine_type = None
        self._engine_name = ""
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
        self._executor = ThreadPoolExecutor(max_workers=max_w)
        self._ensure_engine()
        logger.info(f"调度器已重载配置，并发: {max_w}，引擎: {self._engine_name}")

    # ---------- 引擎 ----------

    def _ensure_engine(self):
        """柔性引擎初始化：找不到引擎时不崩溃，仅记录警告。"""
        if self._engine is not None:
            return
        engine_type, exe_path = discover_engine()
        self._engine_type = engine_type
        self._engine_name = get_engine_display_name(engine_type)
        self._engine = create_engine(exe_path, engine_type)
        if self._engine is None:
            logger.warning("引擎未配置，任务将无法执行。请前往全局设置手动选择引擎路径。")

    # ---------- 任务提交 ----------

    def submit(self, src_path: str) -> Optional[str]:
        """提交单文件任务。返回 task_id 供 UI 层创建表格行。
        注意：不在此处发射"排队中"信号——由 UI 层在插入表格行时一并设置初始状态。"""
        if not self._running:
            self.start()
        file_name = os.path.basename(src_path)
        from core.sandbox import PART_PATTERN
        if not SandboxManager.is_primary_part(file_name):
            if PART_PATTERN.match(file_name):
                logger.info(f"跳过非主分卷: {src_path}")
                return None

        task_id = str(uuid.uuid4())[:8]
        logger.info(f"任务已提交 [id={task_id}]: {src_path}")
        future = self._executor.submit(self._process_task, task_id, src_path)
        self._futures[future] = task_id
        future.add_done_callback(self._on_task_done)
        return task_id

    def submit_existing(self, task_id: str, src_path: str):
        """使用已生成的 task_id 提交任务（递归扫描专用）。
        UI 层负责在调用前创建表格行并设置"排队中"状态。"""
        if not self._running:
            self.start()
        logger.info(f"任务已提交 [id={task_id}]: {src_path}")
        future = self._executor.submit(self._process_task, task_id, src_path)
        self._futures[future] = task_id
        future.add_done_callback(self._on_task_done)

    # ---------- 递归扫描（阶段 4.9）----------

    def scan_directory(self, dir_path: str):
        """
        递归扫描文件夹，将符合条件的压缩包/伪装文件提交到解压队列。
        深度控制由 config.json 中的 max_scan_depth 决定。
        """
        max_depth = int(self._config.get("max_scan_depth", 4))
        logger.info(f"开始递归扫描文件夹（最大深度={max_depth}）: {dir_path}")
        count = self._scan_dir_recursive(Path(dir_path), depth=0, max_depth=max_depth)
        logger.info(f"递归扫描完成，共发现 {count} 个目标文件")

    def _scan_dir_recursive(self, directory: Path, depth: int, max_depth: int) -> int:
        if depth > max_depth:
            logger.debug(f"深度 {depth} 超过上限 {max_depth}，跳过: {directory}")
            return 0

        if not directory.is_dir():
            return 0

        count = 0
        try:
            entries = sorted(directory.iterdir())
        except PermissionError:
            logger.warning(f"无访问权限，跳过目录: {directory}")
            return 0

        for entry in entries:
            if entry.is_file():
                ext = entry.suffix.lower()
                if ext in SCAN_TARGET_EXTS:
                    src_str = str(entry.resolve())
                    # 大小预检查
                    if self._is_file_too_small(src_str):
                        task_id = str(uuid.uuid4())[:8]
                        self.task_status_changed.emit(task_id, entry.name, "已跳过(文件过小)", self._engine_name, "")
                        logger.info(f"[递归扫描] 文件过小，跳过: {entry.name} ({os.path.getsize(src_str)} bytes)")
                        count += 1
                        continue
                    task_id = str(uuid.uuid4())[:8]
                    # 通知 UI 插入新行（"排队中"状态由 UI 层设置）
                    self.task_status_changed.emit(task_id, entry.name, "排队中", self._engine_name, "")
                    self.submit_existing(task_id, src_str)
                    count += 1
                    logger.info(f"[递归扫描] 发现目标文件 (深度={depth}): {entry.name}")
            elif entry.is_dir():
                count += self._scan_dir_recursive(entry, depth + 1, max_depth)

        return count

    @staticmethod
    def _is_file_too_small(file_path: str) -> bool:
        """检查文件是否小于配置的 min_file_size_mb 阈值。"""
        from data.config_manager import ConfigManager
        cfg = ConfigManager()
        min_mb = int(cfg.get("min_file_size_mb", 50))
        min_bytes = min_mb * 1024 * 1024
        try:
            size = os.path.getsize(file_path)
        except OSError:
            return True  # 无法读取大小，保守跳过
        return size < min_bytes

    # ---------- 密码自学习 ----------

    def supply_password(self, task_id: str, password: Optional[str]):
        with self._lock:
            self._supplied_passwords[task_id] = password
            evt = self._password_events.get(task_id)
        if evt:
            evt.set()

    # ---------- 单任务流程 ----------

    def _process_task(self, task_id: str, src_path: str):
        sandbox_dir = None
        success = False
        dest_dir = ""
        cleanup_files: list[Path] = []
        fail_reason = ""

        try:
            src_name = os.path.basename(src_path)
            logger.info(f"[{task_id}] 开始处理: {src_path}")

            # 刷新配置（引擎可能已改变）
            self._ensure_engine()
            # 引擎 None 守卫：未配置引擎则直接失败
            if self._engine is None:
                raise SandboxError("引擎未配置，请前往全局设置手动选择引擎路径")
            name = self._engine_name
            self.task_status_changed.emit(task_id, src_name, "正在提取", name, "")

            # Step 1: 沙箱
            sandbox_dir = self._sandbox.create_sandbox(src_path)

            # Step 2: 加载密码库
            self._pwd_mgr.reload()
            passwords = [r["password"] for r in self._pwd_mgr.get_all()]

            # Step 3: 文件准备（铁律四：伪装包 shutil.copy 到沙箱并重命名为 .zip）
            actual_file = self._sandbox.prepare_file(src_path, sandbox_dir)

            # Step 4: 降维试错链
            dest_dir = str(sandbox_dir / "output")
            self._fallback_extract(task_id, actual_file, dest_dir, passwords, cleanup_files, sandbox_dir)

            # Step 4: 嵌套解压
            self.task_progress.emit(task_id, "正在扫描嵌套压缩包...")
            self._extract_nested(task_id, sandbox_dir, dest_dir, passwords, depth=1)

            # Step 4.3: 容器提权（将最终数据体搬运至根目录，清理沿途空壳）
            self.task_progress.emit(task_id, "正在整理目录结构...")
            SandboxManager.promote_content(Path(dest_dir))

            # Step 4.5: 搬运到用户目标路径（最后一公里）
            self.task_progress.emit(task_id, "正在搬运到目标路径...")
            final_path = self._sandbox.deliver_to_target(src_path, sandbox_dir)
            dest_dir = final_path

            # Step 4.6: 回收沙箱（仅在搬运成功后）
            self._sandbox.cleanup_sandbox(sandbox_dir)
            sandbox_dir = None  # 已回收，防止后续清理

            success = True
            logger.info(f"[{task_id}] [交付成功] 文件已移动至: {final_path}")

        except PasswordRequiredError as e:
            fail_reason = "需要密码，且用户未提供"
            logger.warning(f"[{task_id}] {fail_reason}: {e}")
        except WrongPasswordError as e:
            fail_reason = "密码错误"
            logger.warning(f"[{task_id}] {fail_reason}: {e}")
        except ZipBombDetectedError as e:
            fail_reason = f"Zip Bomb 防御: 深度超过 {MAX_NEST_DEPTH} 层"
            logger.warning(f"[{task_id}] {fail_reason}")
        except ExtractionFailedError as e:
            fail_reason = "文件损坏/格式错误"
            logger.error(f"[{task_id}] {fail_reason}: {e}")
        except SandboxError as e:
            fail_reason = f"沙箱操作失败: {e}"
            logger.error(f"[{task_id}] {fail_reason}")
        except Exception as e:
            fail_reason = f"未知错误: {e}"
            logger.exception(f"[{task_id}] {fail_reason}")
        finally:
            self._cleanup_temp_files(cleanup_files)
            if sandbox_dir is not None and sandbox_dir.exists():
                try:
                    self._sandbox.cleanup_sandbox(sandbox_dir)
                except Exception:
                    pass
            if success:
                self.task_status_changed.emit(
                    task_id, os.path.basename(src_path), "成功",
                    self._engine_name, dest_dir,
                )
            else:
                self.task_status_changed.emit(
                    task_id, os.path.basename(src_path), "失败",
                    self._engine_name, fail_reason,
                )
            self.task_progress.emit(task_id, "任务结束")
            logger.debug(f"[{task_id}] 资源已释放")

    # ---------- 降维试错链（战役一修复：密码问题阻断 Fallback）----------

    def _fallback_extract(
        self, task_id: str, src: Path, dest: str, passwords: list[str],
        cleanup_files: list[Path], sandbox_dir: Path,
    ):
        """
        降维试错链：
        ① 原文件盲解
        ② 若失败且非密码问题，改名 .zip 再试
        ③ 若再失败且非密码问题，repair_extract
        密码问题（PasswordRequiredError / WrongPasswordError）→ 立即中断，绝不进入改名/repair！
        """
        # 链 ①：原文件盲解
        self.task_progress.emit(task_id, "盲解：原文件直接提取...")
        result = self._try_extract_chain(task_id, src, dest, passwords, "原文件盲解")
        if result == "success":
            return
        if result == "password_issue":
            raise PasswordRequiredError(str(src), "密码验证失败于原文件盲解阶段")

        # 链 ②：改名 .zip 再试（仅文件损坏时进入，跳过目录）
        ext = src.suffix.lower()
        if ext in BLIND_EXTRACT_EXTS or not src.is_file():
            if not src.is_file():
                logger.info(f"[{task_id}] 源路径是目录而非文件，直接进入修复链")
            else:
                logger.info(f"[{task_id}] 原文件已是合法后缀 ({ext})，直接进入修复链")
        else:
            self.task_progress.emit(task_id, "盲解：改名 .zip 再试...")
            renamed = sandbox_dir / (src.stem + "_renamed.zip")
            shutil.copy(src, renamed)
            cleanup_files.append(renamed)
            logger.info(f"[{task_id}] shutil.copy → {renamed}")
            result = self._try_extract_chain(task_id, renamed, dest, passwords, "改名.zip")
            if result == "success":
                return
            if result == "password_issue":
                raise PasswordRequiredError(str(src), "密码验证失败于改名.zip 阶段")

        # 链 ③：repair_extract（仅文件损坏时进入）
        self.task_progress.emit(task_id, "降维修复提取 (repair)...")
        archive_to_repair = src if ext in BLIND_EXTRACT_EXTS else (
            cleanup_files[-1] if cleanup_files else src
        )
        result = self._try_repair_chain(task_id, archive_to_repair, dest, passwords)
        if result == "success":
            return
        if result == "password_issue":
            raise PasswordRequiredError(str(src), "密码验证失败于 repair 阶段")

        raise ExtractionFailedError(str(src), -1, "降维试错链全部失败（文件损坏）")

    def _try_extract_chain(
        self, task_id: str, archive: Path, dest: str, passwords: list[str], label: str,
    ) -> str:
        """
        单轮盲解尝试。
        返回 "success" | "password_issue" | "file_damage"
        """
        try:
            self._extract_with_smart_passwords(task_id, archive, dest, passwords, label)
            return "success"
        except (PasswordRequiredError, WrongPasswordError):
            return "password_issue"
        except ExtractionFailedError:
            return "file_damage"

    def _try_repair_chain(
        self, task_id: str, archive: Path, dest: str, passwords: list[str],
    ) -> str:
        try:
            self._repair_with_smart_passwords(task_id, archive, dest, passwords)
            return "success"
        except (PasswordRequiredError, WrongPasswordError):
            return "password_issue"
        except ExtractionFailedError:
            return "file_damage"

    # ---------- 密码智能解压 + 自学习 ----------

    def _extract_with_smart_passwords(
        self, task_id: str, archive: Path, dest: str, passwords: list[str], label: str = "",
    ):
        archive_str = str(archive)
        self.task_status_changed.emit(task_id, "", "验证密码", self._engine_name, "")

        # 1) 无密码
        try:
            self._engine.extract(archive_str, dest, password=None)
            logger.info(f"[{task_id}] {label} 无密码解压成功")
            return
        except PasswordRequiredError:
            logger.info(f"[{task_id}] {label} 需要密码")
        except WrongPasswordError:
            logger.info(f"[{task_id}] {label} 无密码但引擎报告密码错误（异常情况）")
            raise
        except ExtractionFailedError:
            pass

        # 2) 遍历 JSON 密码库
        for i, pwd in enumerate(passwords):
            self.task_progress.emit(task_id, f"{label} 尝试密码 ({i+1}/{len(passwords)})...")
            try:
                self._engine.extract(archive_str, dest, password=pwd)
                logger.info(f"[{task_id}] {label} 密码 #{i+1} 匹配成功")
                return
            except WrongPasswordError:
                continue
            except PasswordRequiredError:
                continue
            except ExtractionFailedError:
                continue

        # 3) 密码自学习（无限重试，直至正确或用户取消）
        self.task_progress.emit(task_id, "密码库全败，等待用户输入密码...")
        self.task_status_changed.emit(task_id, "", "验证密码", self._engine_name, "等待手动输入密码")
        self.password_needed.emit(task_id, archive.name)

        while True:
            user_pwd = self._wait_for_user_password(task_id)
            if user_pwd is None:
                raise PasswordRequiredError(archive_str, "用户取消密码输入")

            try:
                self._engine.extract(archive_str, dest, password=user_pwd)
                logger.info(f"[{task_id}] {label} 用户密码匹配成功")
                self._pwd_mgr.reload()
                self._pwd_mgr.add(user_pwd, "[自动入库]")
                self.task_progress.emit(task_id, "密码已自动入库 ✅")
                return
            except WrongPasswordError:
                # 密码错误 → 通知 UI 弹错误提示，继续循环
                logger.info(f"[{task_id}] {label} 用户密码错误，允许重试")
                self.password_wrong.emit(task_id)
                continue
            except PasswordRequiredError:
                self.password_wrong.emit(task_id)
                continue
            except ExtractionFailedError as e:
                raise ExtractionFailedError(archive_str, e.returncode, str(e))

    def _repair_with_smart_passwords(
        self, task_id: str, archive: Path, dest: str, passwords: list[str],
    ):
        archive_str = str(archive)
        self.task_status_changed.emit(task_id, "", "验证密码", self._engine_name, "")

        try:
            self._engine.repair_extract(archive_str, dest, password=None)
            logger.info(f"[{task_id}] repair 无密码成功")
            return
        except PasswordRequiredError:
            pass
        except WrongPasswordError:
            raise
        except ExtractionFailedError:
            pass

        for i, pwd in enumerate(passwords):
            self.task_progress.emit(task_id, f"repair 尝试密码 ({i+1}/{len(passwords)})...")
            try:
                self._engine.repair_extract(archive_str, dest, password=pwd)
                logger.info(f"[{task_id}] repair 密码 #{i+1} 匹配成功")
                return
            except WrongPasswordError:
                continue
            except PasswordRequiredError:
                continue
            except ExtractionFailedError:
                continue

        self.task_progress.emit(task_id, "repair 密码库全败，等待用户输入密码...")
        self.task_status_changed.emit(task_id, "", "验证密码", self._engine_name, "等待手动输入密码")
        self.password_needed.emit(task_id, archive.name)

        while True:
            user_pwd = self._wait_for_user_password(task_id)
            if user_pwd is None:
                raise PasswordRequiredError(archive_str, "用户取消密码输入")

            try:
                self._engine.repair_extract(archive_str, dest, password=user_pwd)
                logger.info(f"[{task_id}] repair 用户密码匹配成功")
                self._pwd_mgr.reload()
                self._pwd_mgr.add(user_pwd, "[自动入库]")
                self.task_progress.emit(task_id, "密码已自动入库 ✅")
                return
            except WrongPasswordError:
                logger.info(f"[{task_id}] repair 用户密码错误，允许重试")
                self.password_wrong.emit(task_id)
                continue
            except PasswordRequiredError:
                self.password_wrong.emit(task_id)
                continue
            except ExtractionFailedError as e:
                raise ExtractionFailedError(archive_str, e.returncode, str(e))

    # ---------- 线程暂停/恢复 ----------

    def _wait_for_user_password(self, task_id: str) -> Optional[str]:
        evt = threading.Event()
        with self._lock:
            self._password_events[task_id] = evt
            self._supplied_passwords.pop(task_id, None)
        evt.wait(timeout=120)
        with self._lock:
            self._password_events.pop(task_id, None)
            return self._supplied_passwords.pop(task_id, None)

    # ---------- 垃圾回收 ----------

    @staticmethod
    def _cleanup_temp_files(files: list[Path]):
        for f in files:
            try:
                if f.exists():
                    f.unlink()
                    logger.debug(f"垃圾回收已删除: {f}")
            except OSError as e:
                logger.warning(f"垃圾回收失败 ({f}): {e}")

    # ---------- 嵌套解压 ----------

    @staticmethod
    def _has_archives(dir_path: Path) -> bool:
        """
        【停止判定】只检查 dir_path 的直接子项，
        禁止使用 rglob 递归扫描内部，保护 Data Body 不被穿透。
        """
        if not dir_path.is_dir():
            return False
        for entry in dir_path.iterdir():
            if entry.is_file() and entry.suffix.lower() in NESTED_ARCHIVE_EXTS:
                return True
        return False

    def _extract_with_passwords(
        self, task_id: str, archive: Path, dest: str, passwords: list[str],
    ):
        self._extract_with_smart_passwords(task_id, archive, dest, passwords, "嵌套")

    def _extract_nested(
        self, task_id: str, sandbox_dir: Path, base_dest: str,
        passwords: list[str], depth: int,
    ):
        if depth > MAX_NEST_DEPTH:
            raise ZipBombDetectedError(str(sandbox_dir), depth)

        base = Path(base_dest)
        if not base.exists():
            return

        # ===== 【停止判定】只检查当前层级，无压缩包则立即中断 =====
        if not self._has_archives(base):
            return

        archives = [
            child for child in base.iterdir()
            if child.is_file() and child.suffix.lower() in NESTED_ARCHIVE_EXTS
        ]

        for archive in archives:
            if not archive.exists():
                continue

            self.task_progress.emit(task_id, f"处理嵌套压缩包 (层{depth}): {archive.name}")
            parent_dir = archive.parent
            temp_dest = parent_dir / f"_tmp_{archive.stem}"

            ext = archive.suffix.lower()
            rename_workaround = None
            if ext not in {".zip", ".rar", ".7z"}:
                rename_workaround = archive.with_suffix(".zip")
                shutil.copy(archive, rename_workaround)
                working_archive = rename_workaround
                logger.info(f"[{task_id}] 嵌套伪装包重命名: {working_archive}")
            else:
                working_archive = archive

            try:
                self._extract_with_passwords(task_id, working_archive, str(temp_dest), passwords)
            except (PasswordRequiredError, WrongPasswordError):
                logger.warning(f"[{task_id}] 嵌套包需密码，跳过: {archive.name}")
                if rename_workaround and rename_workaround.exists():
                    try: rename_workaround.unlink()
                    except OSError: pass
                if temp_dest.exists():
                    shutil.rmtree(str(temp_dest), ignore_errors=True)
                continue
            except ExtractionFailedError as e:
                logger.warning(f"[{task_id}] 嵌套解压失败: {archive.name} — {e}")
                if rename_workaround and rename_workaround.exists():
                    try: rename_workaround.unlink()
                    except OSError: pass
                if temp_dest.exists():
                    shutil.rmtree(str(temp_dest), ignore_errors=True)
                continue

            # 安全删除中间压缩包
            try:
                archive.unlink()
                logger.info(f"[{task_id}] 已删除中间压缩包: {archive.name}")
            except OSError as e:
                logger.warning(f"[{task_id}] 删除中间压缩包失败 ({archive.name}): {e}")

            # 清理伪装修复文件
            if rename_workaround and rename_workaround.exists():
                try:
                    rename_workaround.unlink()
                except OSError:
                    pass

            # --- 【原子搬运红线】只删压缩包，不拆解 temp_dest 目录结构 ---
            # temp_dest 内部层级原封不动保留，禁止 for child in ... 拍平

        # ---- 递归进入子目录（逐个分支，不重复扫描 base_dest 根）-----
        for subdir in sorted(base.iterdir()):
            if subdir.is_dir():
                self._extract_nested(task_id, sandbox_dir, str(subdir), passwords, depth + 1)

    def _on_task_done(self, future: Future):
        task_id = self._futures.pop(future, None)
        if future.exception():
            logger.error(f"[{task_id}] 任务异常: {future.exception()}")