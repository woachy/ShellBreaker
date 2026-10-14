"""
多线程任务调度器 — 阶段 4.5 / 5.0：解耦架构，委托管线处理解压
"""
import os
import logging
import uuid
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, Future
from typing import Optional

from PySide6.QtCore import QObject, Signal

from core.sandbox import (
    SandboxManager,
    is_secondary_volume,
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
from core.pipeline import (
    ExtractionPipeline,
    MAX_NEST_DEPTH,
    NESTED_ARCHIVE_EXTS,
    BLIND_EXTRACT_EXTS,
)

logger = logging.getLogger("ShellBreaker.TaskQueue")

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
    负责任务并发控制、线程池生命周期、目录递归扫描、用户密码交互协调。
    核心解压逻辑委托给 ExtractionPipeline 处理。
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
        self._lock = threading.RLock()

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
        with self._lock:
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
        with self._lock:
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

    # ---------- 密码自学习与用户交互 ----------

    def supply_password(self, task_id: str, password: Optional[str]):
        with self._lock:
            self._supplied_passwords[task_id] = password
            evt = self._password_events.get(task_id)
        if evt:
            evt.set()

    def _wait_for_user_password(self, task_id: str) -> Optional[str]:
        evt = threading.Event()
        with self._lock:
            self._password_events[task_id] = evt
            self._supplied_passwords.pop(task_id, None)
        evt.wait(timeout=120)
        with self._lock:
            self._password_events.pop(task_id, None)
            return self._supplied_passwords.pop(task_id, None)

    # ---------- 管线构建与单任务处理 ----------

    def _create_pipeline(self) -> ExtractionPipeline:
        """根据当前调度器状态构建 ExtractionPipeline 实例。"""
        self._ensure_engine()
        return ExtractionPipeline(
            engine=self._engine,
            engine_name=self._engine_name,
            sandbox=self._sandbox,
            password_manager=self._pwd_mgr,
            config=self._config,
            on_status_changed=self.task_status_changed.emit,
            on_progress=self.task_progress.emit,
            on_password_needed=self.password_needed.emit,
            on_password_wrong=self.password_wrong.emit,
            wait_for_password=self._wait_for_user_password,
        )

    def _process_task(self, task_id: str, src_path: str):
        """单任务调度入口，委托 ExtractionPipeline 执行。"""
        self._ensure_engine()
        pipeline = self._create_pipeline()
        return pipeline.process(task_id, src_path)

    def _on_task_done(self, future: Future):
        task_id = self._futures.pop(future, None)
        if future.exception():
            logger.error(f"[{task_id}] 任务异常: {future.exception()}")

    # ---------- 向后兼容委托代理 ----------

    def fallback_extract(self, *args, **kwargs):
        return self._create_pipeline().fallback_extract(*args, **kwargs)

    _fallback_extract = fallback_extract

    def try_extract_chain(self, *args, **kwargs):
        return self._create_pipeline().try_extract_chain(*args, **kwargs)

    _try_extract_chain = try_extract_chain

    def try_repair_chain(self, *args, **kwargs):
        return self._create_pipeline().try_repair_chain(*args, **kwargs)

    _try_repair_chain = try_repair_chain

    def extract_with_smart_passwords(self, *args, **kwargs):
        return self._create_pipeline().extract_with_smart_passwords(*args, **kwargs)

    _extract_with_smart_passwords = extract_with_smart_passwords

    def repair_with_smart_passwords(self, *args, **kwargs):
        return self._create_pipeline().repair_with_smart_passwords(*args, **kwargs)

    _repair_with_smart_passwords = repair_with_smart_passwords

    def extract_with_passwords(self, *args, **kwargs):
        return self._create_pipeline().extract_with_passwords(*args, **kwargs)

    _extract_with_passwords = extract_with_passwords

    def extract_nested(self, *args, **kwargs):
        return self._create_pipeline().extract_nested(*args, **kwargs)

    _extract_nested = extract_nested

    def find_and_convert_disguised_archive(self, *args, **kwargs):
        return self._create_pipeline().find_and_convert_disguised_archive(*args, **kwargs)

    _find_and_convert_disguised_archive = find_and_convert_disguised_archive

    def handle_delete_source(self, *args, **kwargs):
        return self._create_pipeline().handle_delete_source(*args, **kwargs)

    _handle_delete_source = handle_delete_source

    @staticmethod
    def cleanup_temp_files(files: list[Path]):
        ExtractionPipeline.cleanup_temp_files(files)

    _cleanup_temp_files = cleanup_temp_files

    @staticmethod
    def has_archives(dir_path: Path, exclude: Optional[set[Path]] = None) -> bool:
        return ExtractionPipeline.has_archives(dir_path, exclude)

    _has_archives = has_archives