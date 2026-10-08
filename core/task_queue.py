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
from core.sandbox import (
    SandboxManager,
    ensure_unique_path,
    PROTECTED_ENDPOINT_EXTS,
    POTENTIAL_DISGUISE_EXTS,
    ACCESSORY_EXTS,
    detect_archive_type,
    is_secondary_volume,
    get_part_group,
    send_to_recycle_bin,
)
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

# 递归扫描目标后缀：标准压缩包 + 常见伪装后缀（白名单）+ 分卷首卷
SCAN_TARGET_EXTS = frozenset({
    # 标准压缩包
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".001",
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
        if is_secondary_volume(file_name):
            logger.info(f"跳过非主分卷: {src_path}")
            return None
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
                if ext in SCAN_TARGET_EXTS or is_secondary_volume(entry.name):
                    src_str = str(entry.resolve())
                    # 分卷检查：若为非首卷分卷，直接跳过并展示状态
                    if is_secondary_volume(entry.name):
                        task_id = str(uuid.uuid4())[:8]
                        self.task_status_changed.emit(task_id, entry.name, "已跳过(非首卷分卷)", self._engine_name, "由首卷统一解压")
                        logger.info(f"[递归扫描] 非首卷分卷，跳过: {entry.name}")
                        count += 1
                        continue
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

            # 自动删除源文件（移至 Windows 回收站）
            if bool(self._config.get("delete_source_after_extract", False)):
                self._handle_delete_source(task_id, src_path, final_path)

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

        had_password_issue = False
        last_failed_err: Optional[ExtractionFailedError] = None

        # 1) 无密码
        try:
            self._engine.extract(archive_str, dest, password=None)
            logger.info(f"[{task_id}] {label} 无密码解压成功")
            return
        except PasswordRequiredError:
            logger.info(f"[{task_id}] {label} 需要密码")
            had_password_issue = True
        except WrongPasswordError:
            logger.info(f"[{task_id}] {label} 无密码但引擎报告密码错误（异常情况）")
            had_password_issue = True
            raise
        except ExtractionFailedError as e:
            last_failed_err = e

        # 2) 遍历 JSON 密码库
        for i, pwd in enumerate(passwords):
            self.task_progress.emit(task_id, f"{label} 尝试密码 ({i+1}/{len(passwords)})...")
            try:
                self._engine.extract(archive_str, dest, password=pwd)
                logger.info(f"[{task_id}] {label} 密码 #{i+1} 匹配成功")
                self._pwd_mgr.record_hit(pwd)
                return
            except WrongPasswordError:
                had_password_issue = True
                continue
            except PasswordRequiredError:
                had_password_issue = True
                continue
            except ExtractionFailedError as e:
                last_failed_err = e
                continue

        # 仅在确实检测到密码需求/错误时才阻塞等待用户输入；若纯属文件损坏/非压缩包，直接抛出 ExtractionFailedError
        if not had_password_issue:
            if last_failed_err is not None:
                raise last_failed_err
            raise ExtractionFailedError(archive_str, -1, f"{label} 解压失败（非压缩包或文件损坏）")

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
                self._pwd_mgr.record_hit(user_pwd)
                if user_pwd not in passwords:
                    passwords.append(user_pwd)
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

        had_password_issue = False
        last_failed_err: Optional[ExtractionFailedError] = None

        try:
            self._engine.repair_extract(archive_str, dest, password=None)
            logger.info(f"[{task_id}] repair 无密码成功")
            return
        except PasswordRequiredError:
            had_password_issue = True
        except WrongPasswordError:
            had_password_issue = True
            raise
        except ExtractionFailedError as e:
            last_failed_err = e

        for i, pwd in enumerate(passwords):
            self.task_progress.emit(task_id, f"repair 尝试密码 ({i+1}/{len(passwords)})...")
            try:
                self._engine.repair_extract(archive_str, dest, password=pwd)
                logger.info(f"[{task_id}] repair 密码 #{i+1} 匹配成功")
                self._pwd_mgr.record_hit(pwd)
                return
            except WrongPasswordError:
                had_password_issue = True
                continue
            except PasswordRequiredError:
                had_password_issue = True
                continue
            except ExtractionFailedError as e:
                last_failed_err = e
                continue

        if not had_password_issue:
            if last_failed_err is not None:
                raise last_failed_err
            raise ExtractionFailedError(archive_str, -1, "repair 解压失败（非压缩包或文件损坏）")

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
                self._pwd_mgr.record_hit(user_pwd)
                if user_pwd not in passwords:
                    passwords.append(user_pwd)
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

    def _handle_delete_source(self, task_id: str, src_path: str, final_path: str):
        """
        解压成功后，安全将源文件（及关联同族分卷）移入 Windows 回收站。
        安全边界检查：
        1. 必须为真实存在的文件
        2. 排除白名单终点程序格式
        3. 交付目录与源文件绝不重叠（源文件绝不能在 final_path 内部）
        4. 获取同族分卷，逐一调用静默回收站 API
        5. 绝不污染 task_status_changed 的 dest_dir 备注
        """
        try:
            src = Path(src_path).resolve()
            if not src.is_file():
                logger.debug(f"[{task_id}] 源文件非普通文件或不存在，跳过删除: {src}")
                return
            if src.suffix.lower() in PROTECTED_ENDPOINT_EXTS:
                logger.warning(f"[{task_id}] 源文件属于受保护终点类型，禁止删除: {src}")
                return

            final_p = Path(final_path).resolve()
            try:
                src.relative_to(final_p)
                logger.warning(f"[{task_id}] 安全防护：源文件位于交付目标目录内，禁止删除: {src}")
                return
            except ValueError:
                pass

            targets = get_part_group(src)
            for t in targets:
                if not t.is_file():
                    continue
                try:
                    t.resolve().relative_to(final_p)
                    logger.warning(f"[{task_id}] 安全防护：分卷文件位于交付目标目录内，禁止删除: {t}")
                    continue
                except ValueError:
                    pass
                ok = send_to_recycle_bin(t)
                if ok:
                    logger.info(f"[{task_id}] 源文件已安全移至回收站: {t.name}")
                else:
                    logger.warning(f"[{task_id}] 源文件移至回收站失败: {t.name}")
        except Exception as e:
            logger.error(f"[{task_id}] 执行自动删除源文件异常: {e}")

    # ---------- 嵌套解压 ----------

    @staticmethod
    def _has_archives(dir_path: Path, exclude: Optional[set[Path]] = None) -> bool:
        """
        【停止判定】只检查 dir_path 的直接子项，
        禁止使用 rglob 递归扫描内部，保护 Data Body 不被穿透。
        """
        if not dir_path.is_dir():
            return False
        for entry in dir_path.iterdir():
            if entry.is_file() and entry.suffix.lower() in NESTED_ARCHIVE_EXTS:
                if exclude and entry in exclude:
                    continue
                return True
        return False

    def _find_and_convert_disguised_archive(
        self,
        base_dir: Path,
        skipped_archives: set[Path],
        task_id: str = "",
        passwords: Optional[list[str]] = None,
        current_depth: int = 1,
    ) -> bool:
        """
        形态驱动的伪装检测与试解压机制（Trial Extraction）：
        1. 当 base 目录已经解压出子目录或多个文件（解包后的游戏/资源本体 Data Body）时：
           仅处理标准的真实压缩包，绝不对散装的图片/视频文件进行伪装下钻，立即视为终点交付。
        2. 当 base 目录仅包含“单个独立孤儿大文件”（或该大文件仅附带了 .txt, .url, .nfo 等说明文件）：
           - 终点白名单保护：.apk (及 .exe, .msi) 无论内部结构为何，绝对禁止作为压缩包拆解；
           - 检查是否已在 skipped_archives 中；
           - 检查伪装后缀（.jpg, .png, .mp4, .pdf 等）及大小阈值 (配置 disguise_min_size_mb，默认 >= 10MB)；
        3. 对齐第一层的试解压机制（Trial Extraction）：
           - 不再以 offset-0 魔数作为硬性门槛！优先使用 detect_archive_type，未识别（如各类图种）则统一使用 .zip；
           - 原位重命名 candidate -> temp_archive（沙箱内同分区原子重命名，零磁盘 I/O）；
           - 尝试解压到 temp_dest：
             * 分支 A（解压成功）：删除 temp_archive，原子上移 temp_dest 下所有文件/目录到 parent_dir，清理 temp_dest，返回 True；
             * 分支 B（解压失败 / 密码用尽用户取消 / 产物为空）：捕获异常，核心回滚还原原文件名，清理 temp_dest，登记到 skipped_archives，返回 False。
        """
        if not base_dir.is_dir():
            return False

        if passwords is None:
            passwords = []

        # 1. 检查是否存在子目录 (Data Body 保护)
        subdirs = [
            e for e in base_dir.iterdir()
            if e.is_dir()
            and e.name not in SandboxManager._SYSTEM_FILES
            and not e.name.startswith("._")
            and not e.name.startswith("_tmp_")
        ]
        if subdirs:
            logger.debug(f"[{task_id}] base 包含子目录，不满足单一大文件伪装形态: {[d.name for d in subdirs]}")
            return False

        # 2. 检查文件列表
        all_files = [
            e for e in base_dir.iterdir()
            if e.is_file()
            and e.name not in SandboxManager._SYSTEM_FILES
            and not e.name.startswith("._")
        ]

        # 过滤掉说明/配件文件 (.txt, .url, .nfo 等)
        non_accessory_files = [
            f for f in all_files
            if f.suffix.lower() not in ACCESSORY_EXTS
        ]

        # 必须是“单个独立孤儿大文件”形态
        if len(non_accessory_files) != 1:
            logger.debug(f"[{task_id}] base 包含 {len(non_accessory_files)} 个非配件文件，非孤儿大文件形态")
            return False

        candidate = non_accessory_files[0]

        if candidate in skipped_archives:
            return False

        # 3. 终点白名单保护：.apk (及 .exe, .msi) 无论内部结构为何，绝对禁止拆解
        if candidate.suffix.lower() in PROTECTED_ENDPOINT_EXTS:
            logger.info(f"[{task_id}] 命中受保护终点白名单 ({candidate.suffix}): {candidate.name}，视为终点交付")
            return False

        # 4. 如果已经是标准压缩包，交给普通循环处理
        if candidate.suffix.lower() in NESTED_ARCHIVE_EXTS:
            return False

        # 5. 后缀检查（.jpg, .png, .mp4, .pdf, .bin 等）
        if candidate.suffix.lower() not in POTENTIAL_DISGUISE_EXTS:
            logger.debug(f"[{task_id}] {candidate.name} 后缀不在伪装候选列表中")
            return False

        # 6. 大小阈值检查（如 > 10MB）
        min_mb = int(self._config.get("disguise_min_size_mb", 10))
        min_bytes = min_mb * 1024 * 1024
        try:
            size = candidate.stat().st_size
        except OSError:
            return False

        if size < min_bytes:
            logger.debug(f"[{task_id}] {candidate.name} 大小 ({size}B) 未达伪装阈值 ({min_bytes}B)")
            return False

        # 7. 确定目标后缀：优先使用魔数识别；若未识别（如各类图种），统一使用 .zip
        target_ext = detect_archive_type(candidate) or ".zip"
        parent_dir = candidate.parent
        temp_archive = ensure_unique_path(parent_dir / f"{candidate.stem}{target_ext}")

        # 8. 原位重命名为目标压缩包格式（零磁盘 I/O）
        orig_candidate = candidate
        try:
            os.replace(orig_candidate, temp_archive)
            logger.info(f"[{task_id}] 孤儿形态试探：原位重命名 {orig_candidate.name} -> {temp_archive.name}")
        except OSError as e:
            logger.error(f"[{task_id}] 原位重命名失败 ({orig_candidate} -> {temp_archive}): {e}")
            return False

        # 9. 针对 temp_archive 进行解压尝试 (Trial Extraction)
        self.task_progress.emit(task_id, f"尝试解压伪装包 (层{current_depth}): {temp_archive.name}")
        temp_dest = ensure_unique_path(parent_dir / f"_tmp_{temp_archive.stem}")

        try:
            self._extract_with_passwords(task_id, temp_archive, str(temp_dest), passwords)
            # 解压产物检查：若 temp_dest 为空，说明解压未产出有效内容，回滚处理
            if not temp_dest.exists() or not any(temp_dest.iterdir()):
                logger.warning(f"[{task_id}] 试解压产物为空 ({temp_archive.name})，触发回滚")
                try:
                    os.replace(temp_archive, orig_candidate)
                except OSError:
                    pass
                if temp_dest.exists():
                    shutil.rmtree(str(temp_dest), ignore_errors=True)
                skipped_archives.add(orig_candidate)
                return False
        except (PasswordRequiredError, WrongPasswordError, ExtractionFailedError) as e:
            logger.warning(f"[{task_id}] 伪装试解压失败/需密码 ({temp_archive.name}): {e}，触发核心回滚")
            # 【核心回滚】：立即将 temp_archive 还原重命名为原始文件名
            try:
                os.replace(temp_archive, orig_candidate)
                logger.info(f"[{task_id}] 已回滚还原为原始文件: {orig_candidate.name}")
            except OSError as re_err:
                logger.error(f"[{task_id}] 回滚重命名失败 ({temp_archive} -> {orig_candidate}): {re_err}")
            # 清理残余的 temp_dest
            if temp_dest.exists():
                shutil.rmtree(str(temp_dest), ignore_errors=True)
            # 将该原始文件路径登记到 skipped_archives
            skipped_archives.add(orig_candidate)
            return False
        except Exception as e:
            logger.error(f"[{task_id}] 伪装试解压遇到异常 ({temp_archive.name}): {e}，触发核心回滚")
            try:
                os.replace(temp_archive, orig_candidate)
            except OSError:
                pass
            if temp_dest.exists():
                shutil.rmtree(str(temp_dest), ignore_errors=True)
            skipped_archives.add(orig_candidate)
            return False

        # 分支 A（解压成功）：说明它确实是伪装包/图种！
        # a. 删除临时压缩包 temp_archive.unlink()
        try:
            temp_archive.unlink()
            logger.info(f"[{task_id}] 伪装包解压成功，已删除临时压缩包: {temp_archive.name}")
        except OSError as e:
            logger.warning(f"[{task_id}] 删除临时压缩包失败 ({temp_archive.name}): {e}")

        # b. 将 temp_dest 下的所有文件/目录原子移动到 parent_dir
        if temp_dest.exists():
            for item in list(temp_dest.iterdir()):
                dest = ensure_unique_path(parent_dir / item.name)
                try:
                    shutil.move(str(item), str(dest))
                    logger.debug(f"[{task_id}] 原子上移: {item.name} → {dest.name}")
                except Exception as e:
                    logger.error(f"[{task_id}] 原子上移失败 ({item.name} → {dest.name}): {e}")
                    raise SandboxError(f"原子上移文件失败: {e}")

            # c. 清理空的 temp_dest
            try:
                shutil.rmtree(str(temp_dest), ignore_errors=True)
                logger.debug(f"[{task_id}] 已清除暂存目录: {temp_dest}")
            except OSError as e:
                logger.warning(f"[{task_id}] 删除暂存目录失败 ({temp_dest}): {e}")

        return True

    def _extract_with_passwords(
        self, task_id: str, archive: Path, dest: str, passwords: list[str],
    ):
        self._extract_with_smart_passwords(task_id, archive, dest, passwords, "嵌套")

    def _extract_nested(
        self, task_id: str, sandbox_dir: Path, base_dest: str,
        passwords: list[str], depth: int,
    ):
        base = Path(base_dest)
        if not base.exists() or not base.is_dir():
            return

        current_depth = depth
        skipped_archives: set[Path] = set()

        # ===== 【多层嵌套消化循环】在当前 base 目录下持续循环处理暴露出的压缩包 =====
        while True:
            if current_depth > MAX_NEST_DEPTH:
                raise ZipBombDetectedError(str(sandbox_dir), current_depth)

            archives = [
                child for child in base.iterdir()
                if child.is_file()
                and child.suffix.lower() in NESTED_ARCHIVE_EXTS
                and child not in skipped_archives
            ]

            if not archives:
                # 检查是否存在单一大文件伪装包并尝试试解压 (Trial Extraction)
                handled = self._find_and_convert_disguised_archive(
                    base, skipped_archives, task_id, passwords, current_depth
                )
                if handled:
                    current_depth += 1
                    continue
                else:
                    break

            for archive in archives:
                if not archive.exists():
                    continue

                self.task_progress.emit(task_id, f"处理嵌套压缩包 (层{current_depth}): {archive.name}")
                parent_dir = archive.parent
                temp_dest = ensure_unique_path(parent_dir / f"_tmp_{archive.stem}")

                # 此时 archive 已具备标准压缩包扩展名，在沙箱内无需任何 copy 重命名
                working_archive = archive

                try:
                    self._extract_with_passwords(task_id, working_archive, str(temp_dest), passwords)
                except (PasswordRequiredError, WrongPasswordError):
                    logger.warning(f"[{task_id}] 嵌套包需密码且用户未提供，跳过: {archive.name}")
                    skipped_archives.add(archive)
                    if temp_dest.exists():
                        shutil.rmtree(str(temp_dest), ignore_errors=True)
                    continue
                except ExtractionFailedError as e:
                    logger.warning(f"[{task_id}] 嵌套解压失败: {archive.name} — {e}")
                    skipped_archives.add(archive)
                    if temp_dest.exists():
                        shutil.rmtree(str(temp_dest), ignore_errors=True)
                    continue

                # 1. 安全删除中间压缩包
                try:
                    archive.unlink()
                    logger.info(f"[{task_id}] 已删除中间压缩包: {archive.name}")
                except OSError as e:
                    logger.warning(f"[{task_id}] 删除中间压缩包失败 ({archive.name}): {e}")

                # 2. 立即将 temp_dest 中的所有文件/子目录使用原子 move 上移到 parent_dir (重名防覆盖)
                if temp_dest.exists():
                    for item in list(temp_dest.iterdir()):
                        dest = ensure_unique_path(parent_dir / item.name)
                        try:
                            shutil.move(str(item), str(dest))
                            logger.debug(f"[{task_id}] 原子上移: {item.name} → {dest.name}")
                        except Exception as e:
                            logger.error(f"[{task_id}] 原子上移失败 ({item.name} → {dest.name}): {e}")
                            raise SandboxError(f"原子上移文件失败: {e}")

                    # 3. 上移完成后，彻底删除空的 temp_dest 目录
                    try:
                        shutil.rmtree(str(temp_dest), ignore_errors=True)
                        logger.debug(f"[{task_id}] 已清除暂存目录: {temp_dest}")
                    except OSError as e:
                        logger.warning(f"[{task_id}] 删除暂存目录失败 ({temp_dest}): {e}")

            current_depth += 1

        # ---- 递归进入子目录（逐个分支，不重复扫描 base_dest 根）-----
        for subdir in sorted(base.iterdir()):
            if subdir.is_dir():
                self._extract_nested(task_id, sandbox_dir, str(subdir), passwords, current_depth)

    def _on_task_done(self, future: Future):
        task_id = self._futures.pop(future, None)
        if future.exception():
            logger.error(f"[{task_id}] 任务异常: {future.exception()}")