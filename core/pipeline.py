"""
解压核心管线 — 解耦自 TaskQueue 的解压执行引擎。
包含降维试错链、嵌套解压、伪装包试探与恢复、密码库自学习、容器提权与安全交付。
"""
import fnmatch
import inspect
import os
import shutil
import logging
import threading
from pathlib import Path
from typing import Optional, Callable

from core.exceptions import (
    PasswordRequiredError,
    WrongPasswordError,
    ZipBombDetectedError,
    ExtractionFailedError,
    SandboxError,
    ExtractionAbortedError,
)
from core.sandbox import (
    SandboxManager,
    ensure_unique_path,
    PROTECTED_ENDPOINT_EXTS,
    POTENTIAL_DISGUISE_EXTS,
    ACCESSORY_EXTS,
    detect_archive_type,
    detect_embedded_archive,
    get_part_group,
    is_secondary_volume,
    send_to_recycle_bin,
    record_rename_journal,
    remove_rename_journal,
)
from core.engine import BaseEngine
from data.password_manager import PasswordManager, extract_contextual_passwords
from data.config_manager import ConfigManager

logger = logging.getLogger("ShellBreaker.Pipeline")

MAX_NEST_DEPTH = 50
NESTED_ARCHIVE_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".001"}
BLIND_EXTRACT_EXTS = {".zip", ".rar", ".7z", ".001", ".tar", ".gz", ".bz2", ".xz"}
FAST_PRECHECK_MIN_SIZE_BYTES = 50 * 1024 * 1024
FAST_PRECHECK_MIN_PASSWORDS = 3


class ExtractionPipeline:
    """
    解压任务执行管线。
    纯逻辑类，与具体的 GUI / 线程调度器解耦，通过回调函数向外报告进度和请求交互。
    """

    def __init__(
        self,
        engine: Optional[BaseEngine] = None,
        engine_name: str = "",
        sandbox: Optional[SandboxManager] = None,
        password_manager: Optional[PasswordManager] = None,
        config: Optional[ConfigManager] = None,
        on_status_changed: Optional[Callable[[str, str, str, str, str], None]] = None,
        on_progress: Optional[Callable[[str, str], None]] = None,
        on_password_needed: Optional[Callable[[str, str], None]] = None,
        on_password_wrong: Optional[Callable[[str], None]] = None,
        wait_for_password: Optional[Callable[[str], Optional[str]]] = None,
        is_aborted: Optional[Callable[[str], bool]] = None,
        on_layer_progress: Optional[Callable[[str, int, int, str], None]] = None,
        wait_for_nested_confirm: Optional[Callable[[str, str, int], str]] = None,
    ):
        self._engine = engine
        self._engine_name = engine_name
        self._sandbox = sandbox if sandbox is not None else SandboxManager()
        self._pwd_mgr = password_manager if password_manager is not None else PasswordManager()
        self._config = config if config is not None else ConfigManager()

        self._on_status_changed = on_status_changed
        self._on_progress = on_progress
        self._on_password_needed = on_password_needed
        self._on_password_wrong = on_password_wrong
        self._wait_for_password = wait_for_password
        self._is_aborted = is_aborted
        self._on_layer_progress = on_layer_progress
        self._wait_for_nested_confirm = wait_for_nested_confirm

        self._counters_lock = threading.Lock()
        self._task_counters: dict[str, dict[str, int]] = {}
        self._task_contextual_pwds: dict[str, set[str]] = {}

    # ---------- 回调封装 ----------

    def _notify_status(self, task_id: str, filename: str, status: str, engine: str, remark: str):
        if self._on_status_changed:
            self._on_status_changed(task_id, filename, status, engine, remark)

    def _notify_progress(self, task_id: str, status_text: str):
        if self._on_progress:
            self._on_progress(task_id, status_text)

    def _notify_layer_progress(
        self,
        task_id: str,
        current_percent: int,
        completed_count: int,
        status_text: str,
    ) -> None:
        """上报实时单层百分比与累计有效解压次数（捕获回调异常，防中断主流程）。"""
        if self._on_layer_progress:
            try:
                pct = max(0, min(100, int(current_percent)))
                self._on_layer_progress(task_id, pct, completed_count, status_text)
            except Exception as e:
                logger.debug(f"[{task_id}] 层级进度回调异常: {e}")

    def _get_counters(self, task_id: str) -> dict[str, int]:
        with self._counters_lock:
            counters = self._task_counters.get(task_id)
            if counters is None:
                counters = {"total": 0, "cycle": 0}
                self._task_counters[task_id] = counters
            return dict(counters)

    def _make_extract_progress_cb(self, task_id: str, archive_name: str) -> Callable[[int], None]:
        def _cb(pct: int) -> None:
            counters = self._get_counters(task_id)
            self._notify_layer_progress(
                task_id,
                pct,
                counters["total"],
                f"正在解压: {archive_name} ({pct}%)",
            )
        return _cb

    def _record_valid_extraction(self, task_id: str) -> int:
        """仅在真正完成一次有效解压且确认有产出后累加 total 与 cycle 计数器。"""
        with self._counters_lock:
            counters = self._task_counters.setdefault(task_id, {"total": 0, "cycle": 0})
            counters["total"] += 1
            counters["cycle"] += 1
            total = counters["total"]
        self._notify_layer_progress(task_id, 100, total, f"已完成第 {total} 次解压")
        return total

    def _restore_layer_progress(self, task_id: str) -> None:
        """在试探/单层失败回滚后恢复上一次已完成的计数显示。"""
        counters = self._get_counters(task_id)
        total = counters["total"]
        if total > 0:
            self._notify_layer_progress(task_id, 100, total, f"已完成第 {total} 次解压")
        else:
            self._notify_layer_progress(task_id, 0, 0, "")

    def _check_nested_limit(self, task_id: str, next_archive_name: str) -> bool:
        """
        检查连续解压周期计数是否达到阈值：
        - 返回 True：继续下钻（若用户选择继续，仅重置 cycle 周期计数器，total 保持累加）
        - 返回 False：用户选择 stop_keep，立即停止继续嵌套下钻，保留当前沙箱产物进入正常交付
        - 抛出 ExtractionAbortedError：用户选择 cancel 或任务被中止
        """
        enable_limit = bool(self._config.get("enable_nested_limit_confirm", True))
        limit = max(1, int(self._config.get("nested_confirm_limit", 10)))
        with self._counters_lock:
            counters = self._task_counters.get(task_id)
            cycle_val = counters["cycle"] if counters is not None else 0
            total_val = counters["total"] if counters is not None else 0

        if (
            enable_limit
            and counters is not None
            and cycle_val >= limit
            and self._wait_for_nested_confirm is not None
        ):
            logger.info(
                f"[{task_id}] 连续解压次数达到阈值 ({cycle_val}/{limit}，累计 {total_val} 次)，"
                f"等待用户确认: {next_archive_name}"
            )
            choice = self._wait_for_nested_confirm(task_id, next_archive_name, total_val)
            self._check_aborted(task_id)
            if choice == "continue":
                with self._counters_lock:
                    if task_id in self._task_counters:
                        self._task_counters[task_id]["cycle"] = 0
                self._notify_status(task_id, "", "正在提取", self._engine_name, "")
                return True
            elif choice == "stop_keep":
                logger.info(f"[{task_id}] 用户选择停止嵌套下钻并保留当前层产物 (累计完成 {total_val} 次)")
                self._notify_status(task_id, "", "正在提取", self._engine_name, "")
                return False
            else:
                raise ExtractionAbortedError(task_id, "用户在嵌套解压次数确认中取消了任务")
        return True

    @staticmethod
    def _supports_kwarg(func: Callable, kwarg: str) -> bool:
        try:
            sig = inspect.signature(func)
            for param in sig.parameters.values():
                if param.kind == inspect.Parameter.VAR_KEYWORD or param.name == kwarg:
                    return True
            return False
        except (TypeError, ValueError):
            return False

    def _engine_extract(
        self,
        archive_str: str,
        dest: str,
        password: Optional[str],
        task_id: str,
        on_progress: Optional[Callable[[int], None]] = None,
    ):
        if on_progress is not None and self._supports_kwarg(self._engine.extract, "on_progress"):
            return self._engine.extract(
                archive_str, dest, password=password, task_id=task_id, on_progress=on_progress
            )
        return self._engine.extract(archive_str, dest, password=password, task_id=task_id)

    def _engine_repair_extract(
        self,
        archive_str: str,
        dest: str,
        password: Optional[str],
        task_id: str,
        on_progress: Optional[Callable[[int], None]] = None,
    ):
        if on_progress is not None and self._supports_kwarg(self._engine.repair_extract, "on_progress"):
            return self._engine.repair_extract(
                archive_str, dest, password=password, task_id=task_id, on_progress=on_progress
            )
        return self._engine.repair_extract(archive_str, dest, password=password, task_id=task_id)

    def _engine_test_password(
        self,
        archive_str: str,
        password: Optional[str],
        task_id: str,
    ) -> bool:
        if self._supports_kwarg(self._engine.test_password, "task_id"):
            return bool(self._engine.test_password(archive_str, password, task_id=task_id))
        return bool(self._engine.test_password(archive_str, password))

    @staticmethod
    def _is_same_volume(path_a: Path, path_b: Path) -> bool:
        """判断两个路径是否位于同一磁盘卷（Windows 按盘符/UNC 共享根判断，POSIX 按 st_dev 判断）。"""
        try:
            res_a = path_a.resolve()
            res_b = path_b.resolve()
            drive_a = os.path.splitdrive(str(res_a))[0].lower()
            drive_b = os.path.splitdrive(str(res_b))[0].lower()
            if drive_a or drive_b:
                return drive_a == drive_b
            probe_a = res_a if res_a.exists() else res_a.parent
            probe_b = res_b if res_b.exists() else res_b.parent
            return os.stat(probe_a).st_dev == os.stat(probe_b).st_dev
        except OSError:
            return False

    def _create_sandbox_for_task(
        self,
        task_id: str,
        src_path: str,
        target_dir: Path,
    ) -> Path:
        """
        同盘隐藏沙箱动态路由：
        - 当目标目录与默认 SANDBOX_ROOT 跨盘（或注入了自定义 Mock Sandbox）且支持 target_dir 时，
          调用 create_sandbox(task_id, target_dir=target_dir) 动态路由至目标同盘隐藏沙箱；
        - 当真实 SandboxManager 的目标目录已与 SANDBOX_ROOT 同盘时，直接使用 SANDBOX_ROOT 即可同盘零拷贝交付；
        - 自动兼容仅接受单参数的旧版 Mock Sandbox。
        """
        if self._supports_kwarg(self._sandbox.create_sandbox, "target_dir"):
            use_target_routing = True
            if isinstance(self._sandbox, SandboxManager):
                import core.sandbox as _sandbox_mod
                if self._is_same_volume(target_dir, _sandbox_mod.SANDBOX_ROOT):
                    use_target_routing = False
            if use_target_routing:
                try:
                    return self._sandbox.create_sandbox(task_id, target_dir=target_dir)
                except TypeError:
                    return self._sandbox.create_sandbox(src_path)
        return self._sandbox.create_sandbox(src_path)

    def _merge_contextual_passwords(
        self,
        task_id: str,
        target_path: str | Path,
        base_passwords: list[str],
    ) -> tuple[list[str], set[str]]:
        """嗅探上下文密码并去重置于候选列表队首（最高优先级），同时记录到任务上下文集合。"""
        try:
            contextual_pwds = extract_contextual_passwords(target_path) or []
        except Exception as e:
            logger.debug(f"[{task_id}] 上下文密码嗅探异常 ({target_path}): {e}")
            contextual_pwds = []

        merged: list[str] = []
        seen: set[str] = set()
        ctx_set: set[str] = set()

        for pwd in contextual_pwds:
            if pwd and pwd not in seen:
                seen.add(pwd)
                merged.append(pwd)
                ctx_set.add(pwd)

        for pwd in base_passwords:
            if pwd and pwd not in seen:
                seen.add(pwd)
                merged.append(pwd)

        if ctx_set:
            with self._counters_lock:
                self._task_contextual_pwds.setdefault(task_id, set()).update(ctx_set)
            logger.info(
                f"[{task_id}] 上下文嗅探到 {len(ctx_set)} 个优先候选密码 ({Path(target_path).name})"
            )
        return merged, ctx_set

    def _record_password_hit(
        self,
        task_id: str,
        pwd: str,
        contextual_set: Optional[set[str]] = None,
    ) -> None:
        """记录命中密码：若来自上下文嗅探则优先调用 record_success(..., tag='自动嗅探')，否则走 record_hit。"""
        is_contextual = False
        if contextual_set and pwd in contextual_set:
            is_contextual = True
        else:
            with self._counters_lock:
                task_ctx = self._task_contextual_pwds.get(task_id)
                if task_ctx and pwd in task_ctx:
                    is_contextual = True

        if is_contextual and hasattr(self._pwd_mgr, "record_success"):
            self._pwd_mgr.record_success(pwd, tag="自动嗅探")
        else:
            self._pwd_mgr.record_hit(pwd)

    def _notify_password_needed(self, task_id: str, archive_basename: str):
        if self._on_password_needed:
            self._on_password_needed(task_id, archive_basename)

    def _notify_password_wrong(self, task_id: str):
        if self._on_password_wrong:
            self._on_password_wrong(task_id)

    def _wait_password(self, task_id: str) -> Optional[str]:
        if self._wait_for_password:
            return self._wait_for_password(task_id)
        return None

    def _check_aborted(self, task_id: str):
        """检查任务是否已被中止，若是则抛出 ExtractionAbortedError。"""
        if self._is_aborted and self._is_aborted(task_id):
            raise ExtractionAbortedError(task_id, "用户手动中止")
        if self._engine is not None and hasattr(self._engine, "is_aborted") and self._engine.is_aborted(task_id):
            raise ExtractionAbortedError(task_id, "用户手动中止")

    # ---------- 核心解压流程 ----------

    def process(self, task_id: str, src_path: str) -> bool:
        """
        执行单任务解压全流程：沙箱创建 -> 准备文件 -> 降维试错 -> 嵌套解压 -> 容器提权 -> 交付 -> 安全清理与删除。
        返回解压是否成功 (bool)。
        """
        sandbox_dir = None
        success = False
        dest_dir = ""
        cleanup_files: list[Path] = []
        fail_reason = ""

        with self._counters_lock:
            self._task_counters[task_id] = {"total": 0, "cycle": 0}
            self._task_contextual_pwds[task_id] = set()

        try:
            self._check_aborted(task_id)
            src_name = os.path.basename(src_path)
            logger.info(f"[{task_id}] 开始处理: {src_path}")

            # 引擎 None 守卫：未配置引擎则直接失败
            if self._engine is None:
                raise SandboxError("引擎未配置，请前往全局设置手动选择引擎路径")
            name = self._engine_name
            self._notify_status(task_id, src_name, "正在提取", name, "")

            # Step 1: 沙箱（同盘隐藏沙箱动态路由）
            self._check_aborted(task_id)
            output_mode = self._config.get("output_mode", "source_directory")
            custom_output_path = str(self._config.get("custom_output_path", "") or "").strip()
            if output_mode in ("custom", "custom_directory") and custom_output_path:
                target_dir = Path(custom_output_path)
            else:
                target_dir = Path(src_path).parent
            sandbox_dir = self._create_sandbox_for_task(task_id, src_path, target_dir)

            # Step 2: 加载密码库 + 上下文密码自动嗅探（队首最高优先级）
            self._pwd_mgr.reload()
            base_passwords = [r["password"] for r in self._pwd_mgr.get_all()]
            passwords, _ = self._merge_contextual_passwords(task_id, src_path, base_passwords)

            # Step 3: 文件准备（铁律四：伪装包 shutil.copy 到沙箱并重命名为 .zip）
            self._check_aborted(task_id)
            actual_file = self._sandbox.prepare_file(src_path, sandbox_dir)

            # Step 4: 降维试错链
            self._check_aborted(task_id)
            dest_dir = str(sandbox_dir / "output")
            self.fallback_extract(task_id, actual_file, dest_dir, passwords, cleanup_files, sandbox_dir)

            # Step 4: 嵌套解压
            self._check_aborted(task_id)
            self._notify_status(task_id, "", "正在提取", self._engine_name, "")
            self._notify_progress(task_id, "正在扫描嵌套压缩包...")
            self.extract_nested(task_id, sandbox_dir, dest_dir, passwords, depth=1)

            # Step 4.3: 容器提权（将最终数据体搬运至根目录，清理沿途空壳）
            self._check_aborted(task_id)
            self._notify_status(task_id, "", "正在提取", self._engine_name, "")
            self._notify_progress(task_id, "正在整理目录结构...")
            SandboxManager.promote_content(Path(dest_dir))

            # Step 4.4: 推广/广告垃圾文件过滤（带防清空安全底线）
            self._check_aborted(task_id)
            if bool(self._config.get("enable_ad_filter", True)):
                ad_patterns = self._config.get("ad_filter_patterns", [])
                if ad_patterns:
                    removed_ads = self.filter_ad_files(Path(dest_dir), ad_patterns)
                    if removed_ads > 0:
                        logger.info(f"[{task_id}] 交付前已清理 {removed_ads} 个推广垃圾文件")

            # Step 4.5: 搬运到用户目标路径（最后一公里）
            self._check_aborted(task_id)
            self._notify_status(task_id, "", "正在提取", self._engine_name, "")
            self._notify_progress(task_id, "正在搬运到目标路径...")
            final_path = self._sandbox.deliver_to_target(src_path, sandbox_dir)
            dest_dir = final_path

            # Step 4.6: 回收沙箱（仅在搬运成功后）
            self._sandbox.cleanup_sandbox(sandbox_dir)
            sandbox_dir = None  # 已回收，防止后续清理

            # 自动删除源文件（三模式：keep / recycle_bin / permanent）
            self.handle_delete_source(task_id, src_path, final_path)

            success = True
            logger.info(f"[{task_id}] [交付成功] 文件已移动至: {final_path}")

        except ExtractionAbortedError as e:
            fail_reason = "用户手动中止"
            logger.info(f"[{task_id}] 任务已被中止: {e}")
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
            with self._counters_lock:
                self._task_counters.pop(task_id, None)
                self._task_contextual_pwds.pop(task_id, None)
            self.cleanup_temp_files(cleanup_files)
            if sandbox_dir is not None and sandbox_dir.exists():
                try:
                    self._sandbox.cleanup_sandbox(sandbox_dir)
                except Exception:
                    pass
            if success:
                self._notify_status(
                    task_id, os.path.basename(src_path), "成功",
                    self._engine_name, dest_dir,
                )
            elif fail_reason == "用户手动中止":
                self._notify_status(
                    task_id, os.path.basename(src_path), "已中止",
                    self._engine_name, "用户手动中止",
                )
            else:
                self._notify_status(
                    task_id, os.path.basename(src_path), "失败",
                    self._engine_name, fail_reason,
                )
            logger.debug(f"[{task_id}] 资源已释放")
            return success

    # ---------- 降维试错链（战役一修复：密码问题阻断 Fallback）----------

    def fallback_extract(
        self,
        task_id: str,
        src: Path,
        dest: str,
        passwords: list[str],
        cleanup_files: list[Path],
        sandbox_dir: Path,
    ):
        """
        降维试错链：
        ① 原文件盲解
        ② 若失败且非密码问题，改名 .zip 再试
        ③ 若再失败且非密码问题，repair_extract
        密码问题（PasswordRequiredError / WrongPasswordError）→ 立即中断，绝不进入改名/repair！
        """
        dest_path = Path(dest)

        # 链 ①：原文件盲解
        self._notify_progress(task_id, "盲解：原文件直接提取...")
        result = self.try_extract_chain(task_id, src, dest, passwords, "原文件盲解")
        if result == "success":
            if dest_path.exists() and any(dest_path.iterdir()):
                self._record_valid_extraction(task_id)
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
            self._notify_progress(task_id, "盲解：改名 .zip 再试...")
            renamed = sandbox_dir / (src.stem + "_renamed.zip")
            shutil.copy(src, renamed)
            cleanup_files.append(renamed)
            logger.info(f"[{task_id}] shutil.copy → {renamed}")
            result = self.try_extract_chain(task_id, renamed, dest, passwords, "改名.zip")
            if result == "success":
                if dest_path.exists() and any(dest_path.iterdir()):
                    self._record_valid_extraction(task_id)
                return
            if result == "password_issue":
                raise PasswordRequiredError(str(src), "密码验证失败于改名.zip 阶段")

        # 链 ③：repair_extract（仅文件损坏时进入）
        self._notify_progress(task_id, "降维修复提取 (repair)...")
        archive_to_repair = src if ext in BLIND_EXTRACT_EXTS else (
            cleanup_files[-1] if cleanup_files else src
        )
        result = self.try_repair_chain(task_id, archive_to_repair, dest, passwords)
        if result == "success":
            if dest_path.exists() and any(dest_path.iterdir()):
                self._record_valid_extraction(task_id)
            return
        if result == "password_issue":
            raise PasswordRequiredError(str(src), "密码验证失败于 repair 阶段")

        # 链 ④：图种/复合伪装偏移剥离提取（仅当检测到偏移 > 0 的内嵌压缩包时触发）
        if src.is_file():
            emb_ext, emb_offset = detect_embedded_archive(src)
            if emb_ext is not None and emb_offset > 0:
                self._notify_progress(task_id, f"图种偏移提取 ({emb_ext})...")
                sliced = ensure_unique_path(sandbox_dir / (src.stem + "_embedded" + emb_ext))
                try:
                    self._slice_embedded_archive(src, emb_offset, sliced)
                    cleanup_files.append(sliced)
                    logger.info(
                        f"[{task_id}] 剥离图种载体前缀 ({emb_offset}B) → {sliced.name}"
                    )
                    result = self.try_extract_chain(task_id, sliced, dest, passwords, "图种偏移提取")
                    if result == "success":
                        if dest_path.exists() and any(dest_path.iterdir()):
                            self._record_valid_extraction(task_id)
                        return
                    if result == "password_issue":
                        raise PasswordRequiredError(str(src), "密码验证失败于图种偏移提取阶段")
                except OSError as slice_err:
                    logger.warning(f"[{task_id}] 剥离图种偏移载荷失败 ({src.name}): {slice_err}")

        raise ExtractionFailedError(str(src), -1, "降维试错链全部失败（文件损坏）")

    def try_extract_chain(
        self, task_id: str, archive: Path, dest: str, passwords: list[str], label: str,
    ) -> str:
        """
        单轮盲解尝试。
        返回 "success" | "password_issue" | "file_damage"
        """
        try:
            self.extract_with_smart_passwords(task_id, archive, dest, passwords, label)
            return "success"
        except (PasswordRequiredError, WrongPasswordError):
            return "password_issue"
        except ExtractionFailedError:
            return "file_damage"

    def try_repair_chain(
        self, task_id: str, archive: Path, dest: str, passwords: list[str],
    ) -> str:
        try:
            self.repair_with_smart_passwords(task_id, archive, dest, passwords)
            return "success"
        except (PasswordRequiredError, WrongPasswordError):
            return "password_issue"
        except ExtractionFailedError:
            return "file_damage"

    # ---------- 密码智能解压 + 自学习 ----------

    def extract_with_smart_passwords(
        self, task_id: str, archive: Path, dest: str, passwords: list[str], label: str = "",
    ):
        self._check_aborted(task_id)
        archive_str = str(archive)
        archive_name = archive.name
        self._notify_status(task_id, "", "正在提取", self._engine_name, "")

        counters = self._get_counters(task_id)
        self._notify_layer_progress(task_id, 0, counters["total"], f"正在解压: {archive_name}")
        progress_cb = self._make_extract_progress_cb(task_id, archive_name)

        had_password_issue = False
        last_failed_err: Optional[ExtractionFailedError] = None

        # 1) 无密码（正常的直接提取阶段）
        try:
            self._engine_extract(archive_str, dest, password=None, task_id=task_id, on_progress=progress_cb)
            logger.info(f"[{task_id}] {label} 无密码解压成功")
            return
        except ExtractionAbortedError:
            raise
        except (PasswordRequiredError, WrongPasswordError):
            logger.info(f"[{task_id}] {label} 需要密码")
            had_password_issue = True
        except ExtractionFailedError:
            # 非密码错误（文件损坏/非压缩包）直接上抛，绝不切换"验证密码"盲跑整本密码库
            raise

        # 2) 大包多密快速预检（先轻量验密再一次性解压，避免 N 次大文件磁盘写入）
        try:
            archive_size = archive.stat().st_size
        except OSError:
            archive_size = 0

        use_fast_precheck = (
            bool(getattr(self._engine, "supports_fast_test", False))
            and len(passwords) > FAST_PRECHECK_MIN_PASSWORDS
            and archive_size > FAST_PRECHECK_MIN_SIZE_BYTES
        )

        if use_fast_precheck:
            self._notify_status(task_id, "", "验证密码", self._engine_name, "")
            matched_pwd: Optional[str] = None
            for i, pwd in enumerate(passwords):
                self._check_aborted(task_id)
                self._notify_progress(task_id, f"快速预检密码 ({i+1}/{len(passwords)})...")
                try:
                    passed = self._engine_test_password(archive_str, pwd, task_id=task_id)
                except ExtractionAbortedError:
                    raise
                except ExtractionFailedError:
                    # 损坏包直接上抛，避免继续空试
                    raise
                except (PasswordRequiredError, WrongPasswordError):
                    had_password_issue = True
                    continue
                if passed:
                    matched_pwd = pwd
                    break

            if matched_pwd is not None:
                logger.info(f"[{task_id}] {label} 快速预检命中密码，执行一次性真实解压")
                self._notify_status(task_id, "", "正在提取", self._engine_name, "")
                try:
                    self._engine_extract(
                        archive_str, dest, password=matched_pwd, task_id=task_id, on_progress=progress_cb
                    )
                    self._record_password_hit(task_id, matched_pwd)
                    return
                except ExtractionAbortedError:
                    raise
                except (WrongPasswordError, PasswordRequiredError):
                    had_password_issue = True
                except ExtractionFailedError:
                    raise
            # 全部候选密码均未通过快速预检：跳过完整解压循环，直接进入手动输入密码流程
        else:
            # 2b) 遍历 JSON 密码库（真正进入密码尝试阶段，才设为“验证密码”）
            if passwords:
                self._notify_status(task_id, "", "验证密码", self._engine_name, "")
            for i, pwd in enumerate(passwords):
                self._check_aborted(task_id)
                self._notify_progress(task_id, f"{label} 尝试密码 ({i+1}/{len(passwords)})...")
                try:
                    self._engine_extract(archive_str, dest, password=pwd, task_id=task_id, on_progress=progress_cb)
                    logger.info(f"[{task_id}] {label} 密码 #{i+1} 匹配成功")
                    self._record_password_hit(task_id, pwd)
                    self._notify_status(task_id, "", "正在提取", self._engine_name, "")
                    return
                except ExtractionAbortedError:
                    raise
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
        self._notify_progress(task_id, "密码库全败，等待用户输入密码...")
        self._notify_status(task_id, "", "验证密码", self._engine_name, "等待手动输入密码")
        self._notify_password_needed(task_id, archive.name)

        while True:
            self._check_aborted(task_id)
            user_pwd = self._wait_password(task_id)
            self._check_aborted(task_id)
            if user_pwd is None:
                raise PasswordRequiredError(archive_str, "用户取消密码输入")

            try:
                self._engine_extract(archive_str, dest, password=user_pwd, task_id=task_id, on_progress=progress_cb)
                logger.info(f"[{task_id}] {label} 用户密码匹配成功")
                self._pwd_mgr.record_hit(user_pwd)
                if user_pwd not in passwords:
                    passwords.append(user_pwd)
                self._notify_progress(task_id, "密码已自动入库 ✅")
                self._notify_status(task_id, "", "正在提取", self._engine_name, "")
                return
            except ExtractionAbortedError:
                raise
            except WrongPasswordError:
                # 密码错误 → 通知 UI 弹错误提示，继续循环
                logger.info(f"[{task_id}] {label} 用户密码错误，允许重试")
                self._notify_password_wrong(task_id)
                continue
            except PasswordRequiredError:
                self._notify_password_wrong(task_id)
                continue
            except ExtractionFailedError as e:
                raise ExtractionFailedError(archive_str, e.returncode, str(e))

    def repair_with_smart_passwords(
        self, task_id: str, archive: Path, dest: str, passwords: list[str],
    ):
        self._check_aborted(task_id)
        archive_str = str(archive)
        archive_name = archive.name
        self._notify_status(task_id, "", "正在提取", self._engine_name, "")

        counters = self._get_counters(task_id)
        self._notify_layer_progress(task_id, 0, counters["total"], f"正在解压: {archive_name}")
        progress_cb = self._make_extract_progress_cb(task_id, archive_name)

        had_password_issue = False
        last_failed_err: Optional[ExtractionFailedError] = None

        try:
            self._engine_repair_extract(archive_str, dest, password=None, task_id=task_id, on_progress=progress_cb)
            logger.info(f"[{task_id}] repair 无密码成功")
            return
        except ExtractionAbortedError:
            raise
        except (PasswordRequiredError, WrongPasswordError):
            had_password_issue = True
        except ExtractionFailedError:
            raise

        if passwords:
            self._notify_status(task_id, "", "验证密码", self._engine_name, "")
        for i, pwd in enumerate(passwords):
            self._check_aborted(task_id)
            self._notify_progress(task_id, f"repair 尝试密码 ({i+1}/{len(passwords)})...")
            try:
                self._engine_repair_extract(archive_str, dest, password=pwd, task_id=task_id, on_progress=progress_cb)
                logger.info(f"[{task_id}] repair 密码 #{i+1} 匹配成功")
                self._record_password_hit(task_id, pwd)
                self._notify_status(task_id, "", "正在提取", self._engine_name, "")
                return
            except ExtractionAbortedError:
                raise
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

        self._notify_progress(task_id, "repair 密码库全败，等待用户输入密码...")
        self._notify_status(task_id, "", "验证密码", self._engine_name, "等待手动输入密码")
        self._notify_password_needed(task_id, archive.name)

        while True:
            self._check_aborted(task_id)
            user_pwd = self._wait_password(task_id)
            self._check_aborted(task_id)
            if user_pwd is None:
                raise PasswordRequiredError(archive_str, "用户取消密码输入")

            try:
                self._engine_repair_extract(archive_str, dest, password=user_pwd, task_id=task_id, on_progress=progress_cb)
                logger.info(f"[{task_id}] repair 用户密码匹配成功")
                self._pwd_mgr.record_hit(user_pwd)
                if user_pwd not in passwords:
                    passwords.append(user_pwd)
                self._notify_progress(task_id, "密码已自动入库 ✅")
                self._notify_status(task_id, "", "正在提取", self._engine_name, "")
                return
            except ExtractionAbortedError:
                raise
            except WrongPasswordError:
                logger.info(f"[{task_id}] repair 用户密码错误，允许重试")
                self._notify_password_wrong(task_id)
                continue
            except PasswordRequiredError:
                self._notify_password_wrong(task_id)
                continue
            except ExtractionFailedError as e:
                raise ExtractionFailedError(archive_str, e.returncode, str(e))

    def extract_with_passwords(
        self, task_id: str, archive: Path, dest: str, passwords: list[str],
    ):
        self.extract_with_smart_passwords(task_id, archive, dest, passwords, "嵌套")

    # ---------- 嵌套解压与伪装包试探 ----------

    def find_and_convert_disguised_archive(
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
           - 检查伪装后缀（.jpg, .png, .mp4, .pdf 等）及大小阈值 (统一配置 min_file_size_mb，默认 >= 50MB，0 表示不限制；已通过魔数确认的图种不受此阈值限制)；
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

        # 6. 图种/复合伪装偏移嗅探 + 大小阈值检查
        embedded_ext, embedded_offset = detect_embedded_archive(candidate)
        is_confirmed_polyglot = embedded_ext is not None and embedded_offset > 0

        val = self._config.get("min_file_size_mb", 50)
        min_mb = int(50 if val is None else val)
        min_bytes = min_mb * 1024 * 1024
        try:
            size = candidate.stat().st_size
        except OSError:
            return False

        if size < min_bytes and not is_confirmed_polyglot:
            logger.debug(f"[{task_id}] {candidate.name} 大小 ({size}B) 未达伪装阈值 ({min_bytes}B)")
            return False

        # 7. 确定目标后缀：优先使用魔数/内嵌图种识别；若未识别，统一使用 .zip
        if is_confirmed_polyglot:
            target_ext = embedded_ext
            logger.info(
                f"[{task_id}] 检测到内嵌图种 ({candidate.name}: offset={embedded_offset}, type={target_ext})"
            )
        else:
            target_ext = embedded_ext or detect_archive_type(candidate) or ".zip"
        parent_dir = candidate.parent
        temp_archive = ensure_unique_path(parent_dir / f"{candidate.stem}{target_ext}")

        # 7.5 反复解压阈值暂停确认检查（在原位重命名前，确保 stop_keep 时沙箱原样保留）
        if not self._check_nested_limit(task_id, candidate.name):
            return False

        # 8. 原位重命名为目标压缩包格式（零磁盘 I/O）+ 写入重命名事务日志
        orig_candidate = candidate
        trial_passwords, trial_ctx = self._merge_contextual_passwords(task_id, orig_candidate, passwords)
        journal_recorded = False
        try:
            try:
                record_rename_journal(task_id, orig_candidate, temp_archive)
                journal_recorded = True
            except Exception as j_err:
                logger.warning(f"[{task_id}] 写入伪装重命名事务日志失败（不阻断流程）: {j_err}")
            os.replace(orig_candidate, temp_archive)
            logger.info(f"[{task_id}] 孤儿形态试探：原位重命名 {orig_candidate.name} -> {temp_archive.name}")
        except OSError as e:
            if journal_recorded:
                try:
                    remove_rename_journal(task_id, temp_archive)
                except Exception:
                    pass
            logger.error(f"[{task_id}] 原位重命名失败 ({orig_candidate} -> {temp_archive}): {e}")
            return False

        # 9. 针对 temp_archive 进行解压尝试 (Trial Extraction)，finally 确保清理事务日志
        try:
            self._check_aborted(task_id)
            self._notify_progress(task_id, f"尝试解压伪装包 (层{current_depth}): {temp_archive.name}")
            temp_dest = ensure_unique_path(parent_dir / f"_tmp_{temp_archive.stem}")

            try:
                try:
                    self.extract_with_passwords(task_id, temp_archive, str(temp_dest), trial_passwords)
                except ExtractionFailedError:
                    if not is_confirmed_polyglot:
                        raise
                    # 图种偏移兜底：若当前引擎无法直接跳过媒体载体前缀，剥离前 embedded_offset 字节后重试
                    sliced_archive = ensure_unique_path(
                        parent_dir / f"_tmp_slice_{temp_archive.stem}{target_ext}"
                    )
                    try:
                        self._slice_embedded_archive(temp_archive, embedded_offset, sliced_archive)
                        logger.info(
                            f"[{task_id}] 嵌套图种剥离媒体前缀 ({embedded_offset}B) 重试: {sliced_archive.name}"
                        )
                        self.extract_with_passwords(
                            task_id, sliced_archive, str(temp_dest), trial_passwords
                        )
                    finally:
                        try:
                            if sliced_archive.exists():
                                sliced_archive.unlink()
                        except OSError:
                            pass
                for learned_pwd in trial_passwords:
                    if learned_pwd not in passwords and learned_pwd not in trial_ctx:
                        passwords.append(learned_pwd)
                # 解压产物检查：若 temp_dest 为空，说明解压未产出有效内容，回滚处理（绝不计入解压次数）
                if not temp_dest.exists() or not any(temp_dest.iterdir()):
                    logger.warning(f"[{task_id}] 试解压产物为空 ({temp_archive.name})，触发回滚")
                    try:
                        os.replace(temp_archive, orig_candidate)
                    except OSError:
                        pass
                    if temp_dest.exists():
                        shutil.rmtree(str(temp_dest), ignore_errors=True)
                    skipped_archives.add(orig_candidate)
                    self._restore_layer_progress(task_id)
                    return False
            except ExtractionAbortedError:
                try:
                    os.replace(temp_archive, orig_candidate)
                except OSError:
                    pass
                if temp_dest.exists():
                    shutil.rmtree(str(temp_dest), ignore_errors=True)
                raise
            except (PasswordRequiredError, WrongPasswordError, ExtractionFailedError) as e:
                logger.warning(f"[{task_id}] 伪装试解压失败/需密码 ({temp_archive.name}): {e}，触发核心回滚")
                # 【核心回滚】：立即将 temp_archive 还原重命名为原始文件名（绝不计入解压次数）
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
                self._restore_layer_progress(task_id)
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
                self._restore_layer_progress(task_id)
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

            # 确认伪装试探成功且产物保留，才计入有效解压次数
            self._record_valid_extraction(task_id)
            return True
        finally:
            if journal_recorded:
                try:
                    remove_rename_journal(task_id, temp_archive)
                except Exception as j_err:
                    logger.warning(f"[{task_id}] 清理伪装重命名事务日志失败: {j_err}")

    @staticmethod
    def _is_native_folder(path: Path) -> bool:
        """
        判断是否为业务原生文件夹：
        定义为 is_dir()，且排除系统隐藏项、排除 _tmp_*、_repaired 等临时解压中转目录。
        """
        if not path.is_dir():
            return False
        name = path.name
        if name in SandboxManager._SYSTEM_FILES:
            return False
        if name.startswith((".", "._", "_tmp_", "_repaired")):
            return False
        return True

    def _get_native_folders(self, dir_path: Path) -> list[Path]:
        """获取目录下的原生业务文件夹列表。"""
        if not dir_path.exists() or not dir_path.is_dir():
            return []
        try:
            return [child for child in dir_path.iterdir() if self._is_native_folder(child)]
        except OSError:
            return []

    def extract_nested(
        self,
        task_id: str,
        sandbox_dir: Path,
        base_dest: str,
        passwords: list[str],
        depth: int,
    ):
        base = Path(base_dest)
        if not base.exists() or not base.is_dir():
            return

        current_depth = depth
        skipped_archives: set[Path] = set()

        # ===== 【底线熔断保护】检测当前 base 是否已存在原生业务文件夹 =====
        # 用户底线：一旦本体解压出来（出现原生业务文件夹），立即停止一切嵌套解压行为，绝不下钻
        native_dirs = self._get_native_folders(base)
        if native_dirs:
            logger.info(
                f"[{task_id}] base 目录下已存在原生业务文件夹 ({[d.name for d in native_dirs]})，"
                f"Data Body 已脱壳，触发绝对熔断，终止嵌套解压"
            )
            return

        # ===== 【多层嵌套消化循环】在当前 base 目录下持续循环处理暴露出的压缩包 =====
        while True:
            self._check_aborted(task_id)
            if current_depth > MAX_NEST_DEPTH:
                raise ZipBombDetectedError(str(sandbox_dir), current_depth)

            archives = [
                child for child in base.iterdir()
                if child.is_file()
                and child.suffix.lower() in NESTED_ARCHIVE_EXTS
                and not is_secondary_volume(child.name)
                and child not in skipped_archives
            ]

            if not archives:
                # 检查是否存在单一大文件伪装包并尝试试解压 (Trial Extraction)
                handled = self.find_and_convert_disguised_archive(
                    base, skipped_archives, task_id, passwords, current_depth
                )
                if handled:
                    # 检查伪装包解压产物中是否诞生了原生业务文件夹
                    native_dirs = self._get_native_folders(base)
                    if native_dirs:
                        logger.info(
                            f"[{task_id}] 伪装解压后检测到原生业务文件夹 "
                            f"({[d.name for d in native_dirs]})，Data Body 已诞生脱壳，"
                            f"触发绝对熔断，终止嵌套解压"
                        )
                        return
                    current_depth += 1
                    continue
                else:
                    break

            for archive in archives:
                self._check_aborted(task_id)
                if not archive.exists():
                    continue

                # 反复解压阈值暂停确认检查：stop_keep 直接结束下钻保留当前沙箱产物进入正常交付
                if not self._check_nested_limit(task_id, archive.name):
                    return

                self._notify_progress(task_id, f"处理嵌套压缩包 (层{current_depth}): {archive.name}")
                parent_dir = archive.parent
                temp_dest = ensure_unique_path(parent_dir / f"_tmp_{archive.stem}")

                # 此时 archive 已具备标准压缩包扩展名，在沙箱内无需任何 copy 重命名
                working_archive = archive
                archive_passwords, nested_ctx = self._merge_contextual_passwords(
                    task_id, working_archive, passwords
                )

                try:
                    self.extract_with_passwords(
                        task_id, working_archive, str(temp_dest), archive_passwords
                    )
                    for learned_pwd in archive_passwords:
                        if learned_pwd not in passwords and learned_pwd not in nested_ctx:
                            passwords.append(learned_pwd)
                except (PasswordRequiredError, WrongPasswordError):
                    logger.warning(f"[{task_id}] 嵌套包需密码且用户未提供，跳过: {archive.name}")
                    skipped_archives.add(archive)
                    if temp_dest.exists():
                        shutil.rmtree(str(temp_dest), ignore_errors=True)
                    self._restore_layer_progress(task_id)
                    continue
                except ExtractionFailedError as e:
                    logger.warning(f"[{task_id}] 嵌套解压失败: {archive.name} — {e}")
                    skipped_archives.add(archive)
                    if temp_dest.exists():
                        shutil.rmtree(str(temp_dest), ignore_errors=True)
                    self._restore_layer_progress(task_id)
                    continue

                has_output = temp_dest.exists() and any(temp_dest.iterdir())

                # 1. 安全删除中间压缩包及其位于沙箱内的所有同族分卷
                sandbox_resolved = sandbox_dir.resolve()
                part_group = get_part_group(archive)
                if archive.resolve() not in [p.resolve() for p in part_group]:
                    part_group.append(archive)
                for part in part_group:
                    try:
                        part.resolve().relative_to(sandbox_resolved)
                    except ValueError:
                        logger.warning(f"[{task_id}] 安全防护：跳过沙箱外分卷删除: {part}")
                        continue
                    try:
                        if part.exists():
                            part.unlink()
                            logger.info(f"[{task_id}] 已删除中间压缩包/分卷: {part.name}")
                    except OSError as e:
                        logger.warning(f"[{task_id}] 删除中间压缩包/分卷失败 ({part.name}): {e}")

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

                # 3.5 确认确实有有效产出后才累加双计数器并上报进度
                if has_output:
                    self._record_valid_extraction(task_id)
                else:
                    self._restore_layer_progress(task_id)

                # 4. 关键底线：检查当前解压产物中是否诞生了原生业务文件夹，若是则立即绝对熔断！
                native_dirs = self._get_native_folders(base)
                if native_dirs:
                    logger.info(
                        f"[{task_id}] 解压 {archive.name} 后检测到原生业务文件夹 "
                        f"({[d.name for d in native_dirs]})，Data Body 已诞生脱壳，"
                        f"触发绝对熔断，终止后续一切解压行为"
                    )
                    return

            current_depth += 1

        # 注意：彻底移除原先向子目录无脑 extract_nested 递归遍历的代码
        # 嵌套解压仅在外壳层（纯套娃壳）流转，绝不穿透业务本体！

    # ---------- 推广/广告垃圾文件过滤 ----------

    @staticmethod
    def filter_ad_files(output_dir: Path | str, patterns: list[str]) -> int:
        """
        递归清理 output_dir 中命中黑名单通配符的推广/广告垃圾文件（不区分大小写）。
        核心安全底线：仅当目录内仍有至少 1 个非黑名单主业务文件时才执行删除；
        若全部文件都被命中，绝不清空产物目录，记录 warning 并返回 0。
        删除后自底向上清理因此变空的子目录（绝不删除 output_dir 本身）。
        返回实际删除的广告文件数量。
        """
        root = Path(output_dir)
        if not root.exists() or not root.is_dir() or not patterns:
            return 0

        norm_patterns = [str(p).strip().lower() for p in patterns if str(p).strip()]
        if not norm_patterns:
            return 0

        all_files: list[Path] = []
        for item in root.rglob("*"):
            try:
                if item.is_file():
                    all_files.append(item)
            except OSError:
                continue

        if not all_files:
            return 0

        matched_ads: list[Path] = []
        kept_files: list[Path] = []
        for f in all_files:
            fname_lower = f.name.lower()
            if any(fnmatch.fnmatch(fname_lower, pat) for pat in norm_patterns):
                matched_ads.append(f)
            else:
                kept_files.append(f)

        if not matched_ads:
            return 0

        # 核心安全底线：至少保留 1 个主业务文件，绝不清空整个产物目录
        if len(kept_files) < 1:
            logger.warning(
                f"推广文件过滤安全底线触发：目录 '{root}' 内全部 {len(matched_ads)} 个文件均命中黑名单，"
                f"已中止清理以防止清空产物目录"
            )
            return 0

        deleted_count = 0
        for ad_file in matched_ads:
            try:
                ad_file.unlink()
                deleted_count += 1
                logger.info(f"已过滤推广垃圾文件: {ad_file.name}")
            except OSError as e:
                logger.warning(f"过滤推广垃圾文件失败 ({ad_file}): {e}")

        # 自底向上清理因此变空的子目录（绝不删 output_dir 本身）
        if deleted_count > 0:
            root_resolved = root.resolve()
            for dirpath, _dirnames, _filenames in os.walk(str(root), topdown=False):
                cur = Path(dirpath)
                try:
                    if cur.resolve() == root_resolved:
                        continue
                    if not any(cur.iterdir()):
                        cur.rmdir()
                        logger.debug(f"推广过滤后已清理空子目录: {cur}")
                except OSError:
                    continue

        return deleted_count

    @staticmethod
    def _slice_embedded_archive(src: Path, offset: int, dest: Path) -> None:
        """流式剥离图种媒体载体前缀（前 offset 字节），输出纯偏移 0 的压缩包副本。"""
        chunk_size = 1024 * 1024
        with open(src, "rb") as fin, open(dest, "wb") as fout:
            fin.seek(offset)
            while True:
                buf = fin.read(chunk_size)
                if not buf:
                    break
                fout.write(buf)

    # ---------- 安全删除源文件与回收（三模式）----------

    def handle_delete_source(
        self,
        task_id: str,
        src_path: str | Path,
        final_path: Optional[str | Path] = None,
    ):
        """
        解压成功后按配置的 delete_source_mode 处理源文件及关联同族分卷：
        - "keep"：保留源文件，不执行删除；
        - "recycle_bin"：静默移入 Windows 回收站；
        - "permanent"：直接物理删除（unlink）；
        若 delete_source_mode 未配置，向后兼容回退到 delete_source_after_extract。
        """
        try:
            raw_mode = self._config.get("delete_source_mode")
            if raw_mode in ("keep", "recycle_bin", "permanent"):
                mode = raw_mode
            else:
                mode = (
                    "recycle_bin"
                    if bool(self._config.get("delete_source_after_extract", False))
                    else "keep"
                )

            if mode == "keep":
                return

            src = Path(src_path).resolve()
            if not src.is_file():
                logger.debug(f"[{task_id}] 源文件非普通文件或不存在，跳过删除: {src}")
                return
            if src.suffix.lower() in PROTECTED_ENDPOINT_EXTS:
                logger.warning(f"[{task_id}] 源文件属于受保护终点类型，禁止删除: {src}")
                return

            final_p = Path(final_path).resolve() if final_path is not None else None
            if final_p is not None:
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
                if final_p is not None:
                    try:
                        t.resolve().relative_to(final_p)
                        logger.warning(f"[{task_id}] 安全防护：分卷文件位于交付目标目录内，禁止删除: {t}")
                        continue
                    except ValueError:
                        pass

                if mode == "recycle_bin":
                    ok = send_to_recycle_bin(t)
                    if ok:
                        logger.info(f"[{task_id}] 源文件已安全移至回收站: {t.name}")
                    else:
                        logger.warning(f"[{task_id}] 源文件移至回收站失败: {t.name}")
                elif mode == "permanent":
                    try:
                        t.unlink()
                        logger.info(f"[{task_id}] 源文件已永久物理删除: {t.name}")
                    except OSError as unlink_err:
                        logger.warning(f"[{task_id}] 源文件永久物理删除失败 ({t.name}): {unlink_err}")
        except Exception as e:
            logger.error(f"[{task_id}] 执行自动删除源文件异常: {e}")

    # ---------- 辅助判断与清理 ----------

    @staticmethod
    def has_archives(dir_path: Path, exclude: Optional[set[Path]] = None) -> bool:
        """
        【停止判定】只检查 dir_path 的直接子项，
        禁止使用 rglob 递归扫描内部，保护 Data Body 不被穿透。
        """
        if not dir_path.is_dir():
            return False
        for entry in dir_path.iterdir():
            if (
                entry.is_file()
                and entry.suffix.lower() in NESTED_ARCHIVE_EXTS
                and not is_secondary_volume(entry.name)
            ):
                if exclude and entry in exclude:
                    continue
                return True
        return False

    @staticmethod
    def cleanup_temp_files(files: list[Path]):
        for f in files:
            try:
                if f.exists():
                    f.unlink()
                    logger.debug(f"垃圾回收已删除: {f}")
            except OSError as e:
                logger.warning(f"垃圾回收失败 ({f}): {e}")

    # ---------- 兼容别名映射 ----------
    _fallback_extract = fallback_extract
    _try_extract_chain = try_extract_chain
    _try_repair_chain = try_repair_chain
    _extract_with_smart_passwords = extract_with_smart_passwords
    _repair_with_smart_passwords = repair_with_smart_passwords
    _extract_with_passwords = extract_with_passwords
    _extract_nested = extract_nested
    _find_and_convert_disguised_archive = find_and_convert_disguised_archive
    _handle_delete_source = handle_delete_source
    _has_archives = has_archives
    _cleanup_temp_files = cleanup_temp_files
    _process_task = process
