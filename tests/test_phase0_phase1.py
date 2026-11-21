"""
Batch 1 (Phase 0 + Phase 1) 完整集成与单元测试套件。

全套测试支持 QT_QPA_PLATFORM=offscreen 离屏运行，无外部 Bandizip / WinRAR 依赖。
覆盖范围：
- Phase 0：robust_rmtree、ConfigManager/PasswordManager 原子写盘与新默认配置、
  Bug #1~#6 回归验证。
- Phase 1：BaseEngine 流式百分比 + 沙箱体积旁路采样、双计数器精准计次、
  嵌套阈值暂停三态（continue 二次触发 total==4 / stop_keep 交付 / cancel 销毁沙箱）、
  ExtractionProgressWidget、TasksPage 5 列与 StatusPillDelegate、
  SettingsPage 高级选项联动持久化、MainWindow + NestedLimitConfirmDialog 跨线程联动。
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from PySide6.QtCore import QEventLoop, QModelIndex, QRect, Qt, QTimer
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QStyleOptionViewItem

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.sandbox as sandbox_mod
import data.config_manager as cfg_mod
from core.engine import BaseEngine, WinRAREngine
from core.exceptions import (
    ExtractionAbortedError,
    ExtractionFailedError,
    PasswordRequiredError,
    WrongPasswordError,
)
from core.pipeline import ExtractionPipeline
from core.sandbox import SandboxManager, robust_rmtree
from core.task_queue import TaskQueue
from data.config_manager import DEFAULT_CONFIG, ConfigManager
from data.password_manager import PasswordManager
from ui.main_window import MainWindow, NestedLimitConfirmDialog
from ui.pages.settings_page import SettingsPage
from ui.pages.tasks_page import HEADERS, TasksPage
from ui.theme.theme_manager import get_current_theme
from ui.widgets.extraction_progress_bar import ExtractionProgressWidget
from ui.widgets.status_pill import StatusPillDelegate, status_palette


# ==================== 通用辅助：离屏 Mock 解压引擎 ====================

class _FakeZipEngine(BaseEngine):
    """基于 Python zipfile 的纯内存/磁盘解压引擎，零外部依赖，支持密码与进度回调。"""

    def __init__(
        self,
        exe_path: str | Path,
        *,
        password_map: dict[str, str] | None = None,
        fail_names: set[str] | None = None,
        progress_steps: list[int] | None = None,
    ):
        super().__init__(exe_path)
        self.password_map = dict(password_map or {})
        self.fail_names = set(fail_names or set())
        self.progress_steps = list(progress_steps or [25, 70, 100])
        self.extract_calls: list[tuple[str, str | None]] = []

    def extract(self, archive, dest, password=None, timeout=300, task_id=None, on_progress=None) -> Path:
        archive_path = Path(archive)
        dest_path = Path(dest)
        dest_path.mkdir(parents=True, exist_ok=True)
        name = archive_path.name
        self.extract_calls.append((name, password))

        if task_id and self.is_aborted(task_id):
            raise ExtractionAbortedError(task_id, "任务已被中止")

        if name in self.fail_names:
            raise ExtractionFailedError(str(archive_path), 2, "corrupt archive")

        if name in self.password_map:
            expected = self.password_map[name]
            if not password:
                raise PasswordRequiredError(str(archive_path), "password required")
            if password != expected:
                raise WrongPasswordError(str(archive_path), password, "wrong password")

        if not zipfile.is_zipfile(archive_path):
            raise ExtractionFailedError(str(archive_path), 2, "not a zip archive")

        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(dest_path)

        if on_progress is not None:
            for pct in self.progress_steps:
                on_progress(pct)
        return dest_path

    def repair_extract(self, archive, dest, password=None, timeout=600, task_id=None, on_progress=None) -> Path:
        return self.extract(archive, dest, password=password, timeout=timeout, task_id=task_id, on_progress=on_progress)


def _create_nested_zip_chain(root_dir: Path, layers: int, leaf_name: str = "final_payload.txt") -> Path:
    """构造 layers 层嵌套 ZIP：layer_1.zip -> layer_2.zip -> ... -> layer_{layers}.zip -> leaf_name。"""
    current_payload = root_dir / leaf_name
    current_payload.write_text("shellbreaker payload ok", encoding="utf-8")

    # 从最内层向外逐层打包
    for idx in range(layers, 0, -1):
        zpath = root_dir / f"layer_{idx}.zip"
        with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.write(current_payload, arcname=current_payload.name)
        current_payload.unlink()
        current_payload = zpath
    return current_payload


def _pump_qt_events(timeout_ms: int = 50) -> None:
    app = QApplication.instance()
    if app is not None:
        loop = QEventLoop()
        QTimer.singleShot(timeout_ms, loop.quit)
        loop.exec()
        app.processEvents()


# ==================== Phase 0 测试 ====================

class TestPhase0RobustnessAndAtomicity(unittest.TestCase):
    """Phase 0：robust_rmtree、ConfigManager/PasswordManager 原子写盘与新默认配置。"""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="sb_p0_"))

    def tearDown(self):
        robust_rmtree(self.scratch)

    def test_robust_rmtree_nonexistent_path(self):
        missing = self.scratch / "does_not_exist"
        self.assertTrue(robust_rmtree(missing))

    def test_robust_rmtree_readonly_files(self):
        target = self.scratch / "ro_tree"
        sub = target / "nested"
        sub.mkdir(parents=True)
        ro_file = sub / "locked.txt"
        ro_file.write_text("readonly", encoding="utf-8")
        os.chmod(ro_file, stat.S_IREAD)

        self.assertTrue(robust_rmtree(target))
        self.assertFalse(target.exists())

    def test_robust_rmtree_exponential_backoff_retry(self):
        target = self.scratch / "retry_dir"
        target.mkdir()
        (target / "a.txt").write_text("x", encoding="utf-8")

        real_rmtree = shutil.rmtree
        calls = {"n": 0}
        sleep_calls: list[float] = []

        def flaky_rmtree(p, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise PermissionError("simulated handle lock")
            return real_rmtree(p, *args, **kwargs)

        with patch("core.sandbox.shutil.rmtree", side_effect=flaky_rmtree), \
             patch("core.sandbox.time.sleep", side_effect=lambda d: sleep_calls.append(d)):
            ok = robust_rmtree(target, max_retries=4)

        self.assertTrue(ok)
        self.assertFalse(target.exists())
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleep_calls, [0.1, 0.2])

    def test_robust_rmtree_returns_false_after_exhausting_retries(self):
        target = self.scratch / "stuck_dir"
        target.mkdir()
        sleep_calls: list[float] = []

        with patch("core.sandbox.shutil.rmtree", side_effect=PermissionError("always locked")), \
             patch("core.sandbox.time.sleep", side_effect=lambda d: sleep_calls.append(d)):
            ok = robust_rmtree(target, max_retries=2)

        self.assertFalse(ok)
        self.assertTrue(target.exists())
        self.assertEqual(sleep_calls, [0.1, 0.2])

    def test_config_manager_new_defaults_and_atomic_save(self):
        self.assertIs(DEFAULT_CONFIG["enable_nested_limit_confirm"], True)
        self.assertEqual(DEFAULT_CONFIG["nested_confirm_limit"], 10)
        self.assertEqual(DEFAULT_CONFIG["min_file_size_mb"], 50)
        self.assertNotIn("disguise_min_size_mb", DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["max_green_cells"], 5)

        cfg = ConfigManager()
        orig_path = cfg_mod.CONFIG_PATH
        orig_data = dict(cfg._data)
        orig_loaded = cfg._loaded
        tmp_cfg = self.scratch / "config.json"
        try:
            cfg_mod.CONFIG_PATH = tmp_cfg
            cfg._loaded = False
            cfg._data = {}

            # 首次加载生成默认配置
            self.assertEqual(cfg.get("nested_confirm_limit"), 10)
            self.assertEqual(cfg.get("min_file_size_mb"), 50)
            self.assertEqual(cfg.get("max_green_cells"), 5)
            self.assertTrue(cfg.get("enable_nested_limit_confirm"))
            self.assertTrue(tmp_cfg.exists())

            # 原子写盘验证：无残留 .tmp 文件（包含 0 值不被误吞）
            cfg.set("nested_confirm_limit", 15)
            cfg.set("min_file_size_mb", 0)
            cfg.save()
            self.assertEqual(list(self.scratch.glob("*.tmp")), [])
            disk_data = json.loads(tmp_cfg.read_text(encoding="utf-8"))
            self.assertEqual(disk_data["nested_confirm_limit"], 15)
            self.assertEqual(disk_data["min_file_size_mb"], 0)

            cfg.set("min_file_size_mb", 25)
            cfg.save()
            disk_data = json.loads(tmp_cfg.read_text(encoding="utf-8"))
            self.assertEqual(disk_data["min_file_size_mb"], 25)

            # 模拟写盘途中异常：原文件内容不受破坏且临时文件被清理
            with patch("data.config_manager.os.replace", side_effect=OSError("disk error")):
                cfg.set("nested_confirm_limit", 99)
                with self.assertRaises(OSError):
                    cfg.save()
            self.assertEqual(list(self.scratch.glob("*.tmp")), [])
            disk_after = json.loads(tmp_cfg.read_text(encoding="utf-8"))
            self.assertEqual(disk_after["nested_confirm_limit"], 15)
        finally:
            cfg_mod.CONFIG_PATH = orig_path
            cfg._data = orig_data
            cfg._loaded = orig_loaded

    def test_password_manager_atomic_persist_utf8_and_no_tmp_leak(self):
        mgr = PasswordManager()
        orig_file = mgr._file
        orig_cache = [dict(x) for x in mgr._cache]
        orig_loaded = mgr._loaded
        tmp_pwd = self.scratch / "passwords.json"
        try:
            mgr._file = tmp_pwd
            mgr._cache = []
            mgr._loaded = True

            mgr.add("中文密码测试_原子写盘", "[Phase0]")
            self.assertTrue(tmp_pwd.exists())
            self.assertEqual(list(self.scratch.glob("*.tmp")), [])

            raw_text = tmp_pwd.read_text(encoding="utf-8")
            self.assertIn("中文密码测试_原子写盘", raw_text)  # ensure_ascii=False

            with patch("data.password_manager.os.replace", side_effect=OSError("replace fail")):
                with self.assertRaises(OSError):
                    mgr.add("will_fail", "[Fail]")
            self.assertEqual(list(self.scratch.glob("*.tmp")), [])
            saved = json.loads(tmp_pwd.read_text(encoding="utf-8"))
            self.assertEqual([x["password"] for x in saved], ["中文密码测试_原子写盘"])
        finally:
            mgr._file = orig_file
            mgr._cache = orig_cache
            mgr._loaded = orig_loaded


class TestPhase0BugFixes(unittest.TestCase):
    """Phase 0：Bug #1 ~ #6 逐项回归测试。"""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="sb_bugs_"))
        self.sandbox_root = self.scratch / "sandboxes"
        self.sandbox_root.mkdir()
        self._orig_sandbox_root = sandbox_mod.SANDBOX_ROOT
        sandbox_mod.SANDBOX_ROOT = self.sandbox_root

        self.cfg = ConfigManager()
        self._orig_cfg = self.cfg.get_all()
        self.cfg.set("output_mode", "custom")
        self.out_dir = self.scratch / "output_target"
        self.out_dir.mkdir()
        self.cfg.set("custom_output_path", str(self.out_dir))
        self.cfg.set("min_file_size_mb", 0)
        self.cfg.set("delete_source_after_extract", False)
        self.cfg.set("enable_nested_limit_confirm", False)

        self.fake_exe = self.scratch / "fake_engine.exe"
        self.fake_exe.write_bytes(b"MZ")

    def tearDown(self):
        sandbox_mod.SANDBOX_ROOT = self._orig_sandbox_root
        for k, v in self._orig_cfg.items():
            self.cfg.set(k, v)
        self.cfg.save()
        robust_rmtree(self.scratch)

    def test_bug1_terminal_remark_not_overwritten_by_task_end_or_late_progress(self):
        """Bug #1：终态后 COL_REMARK 保留交付路径/失败原因，绝不被 '任务结束' 或后续进度覆盖。"""
        win = MainWindow()
        try:
            # 场景 A：成功终态
            win._on_task_status_changed("t_ok", "demo.zip", "正在提取", "Bandizip", "")
            win._on_task_progress("t_ok", "盲解：原文件直接提取...")
            row_ok = win._task_rows["t_ok"]
            self.assertEqual(win._get_cell_text(row_ok, TasksPage.COL_REMARK), "盲解：原文件直接提取...")

            dest_str = str(self.out_dir / "demo")
            win._on_task_status_changed("t_ok", "demo.zip", "成功", "Bandizip", dest_str)
            self.assertEqual(win._get_cell_text(row_ok, TasksPage.COL_REMARK), dest_str)

            # 后续若迟到发射 "任务结束" / 普通进度 / 层级进度，均不可覆盖终态备注
            win._on_task_progress("t_ok", "任务结束")
            win._on_task_progress("t_ok", "迟到的进度文本")
            win._on_task_layer_progress("t_ok", 50, 1, "迟到的层级进度")
            self.assertEqual(win._get_cell_text(row_ok, TasksPage.COL_REMARK), dest_str)

            # 场景 B：失败终态
            win._on_task_status_changed("t_fail", "bad.zip", "失败", "Bandizip", "文件损坏/格式错误")
            row_fail = win._task_rows["t_fail"]
            win._on_task_progress("t_fail", "任务结束")
            win._on_task_layer_progress("t_fail", 10, 0, "任务结束")
            self.assertEqual(win._get_cell_text(row_fail, TasksPage.COL_REMARK), "文件损坏/格式错误")
        finally:
            win.close()

        # 同时验证 ExtractionPipeline.process 自身不再发射 "任务结束"
        src_zip = self.scratch / "simple.zip"
        with zipfile.ZipFile(src_zip, "w") as zf:
            zf.writestr("hello.txt", "world")
        progress_msgs: list[str] = []
        statuses: list[tuple[str, str, str, str, str]] = []
        engine = _FakeZipEngine(self.fake_exe)
        pipeline = ExtractionPipeline(
            engine=engine,
            engine_name="FakeEngine",
            sandbox=SandboxManager(),
            config=self.cfg,
            on_status_changed=lambda *a: statuses.append(a),
            on_progress=lambda tid, txt: progress_msgs.append(txt),
        )
        ok = pipeline.process("t_pipe", str(src_zip))
        self.assertTrue(ok)
        self.assertNotIn("任务结束", progress_msgs)
        self.assertEqual(statuses[-1][2], "成功")
        self.assertTrue(statuses[-1][4].endswith("simple"))

    def test_bug2_scan_directory_registers_src_path_in_queue_and_main_window(self):
        """Bug #2：scan_directory() 扫描入队的任务正确记录 src_path（task_registered + _task_data）。"""
        scan_dir = self.scratch / "scan_root"
        sub = scan_dir / "nested"
        sub.mkdir(parents=True)
        main_zip = sub / "pack.zip"
        sec_vol = sub / "pack.part02.rar"
        main_zip.write_bytes(b"PK\x03\x04" + b"0" * 64)
        sec_vol.write_bytes(b"Rar!" + b"0" * 64)

        win = MainWindow()
        try:
            registered: list[tuple[str, str]] = []
            win._task_queue.task_registered.connect(lambda tid, sp: registered.append((tid, sp)))

            with patch.object(win._task_queue, "submit_existing") as mock_submit:
                win._task_queue.scan_directory(str(scan_dir))
                _pump_qt_events(40)
                mock_submit.assert_called_once()

            self.assertEqual(len(registered), 2)
            reg_paths = {Path(sp).resolve() for _, sp in registered}
            self.assertIn(main_zip.resolve(), reg_paths)
            self.assertIn(sec_vol.resolve(), reg_paths)

            for tid, sp in registered:
                self.assertEqual(Path(win._task_queue.get_task_src_path(tid)).resolve(), Path(sp).resolve())
                self.assertIn(tid, win._task_data)
                self.assertEqual(Path(win._task_data[tid]["src_path"]).resolve(), Path(sp).resolve())
        finally:
            win.close()

    def test_bug3_non_password_extraction_failure_never_runs_password_library(self):
        """Bug #3：非密码 ExtractionFailedError 不盲跑密码库、不闪现 '验证密码'。"""
        corrupt = self.scratch / "corrupt.zip"
        corrupt.write_bytes(b"not a real archive at all")

        engine = _FakeZipEngine(self.fake_exe, fail_names={"corrupt.zip"})
        statuses: list[str] = []
        pipeline = ExtractionPipeline(
            engine=engine,
            engine_name="FakeEngine",
            sandbox=SandboxManager(),
            config=self.cfg,
            on_status_changed=lambda tid, fn, st, eng, rmk: statuses.append(st),
        )

        with self.assertRaises(ExtractionFailedError):
            pipeline.extract_with_smart_passwords(
                "t_bug3", corrupt, str(self.scratch / "d"), ["pwd1", "pwd2", "pwd3"], "原文件盲解"
            )

        # 仅尝试无密码 1 次，绝不遍历密码库，绝不出现 "验证密码"
        self.assertEqual(engine.extract_calls, [("corrupt.zip", None)])
        self.assertNotIn("验证密码", statuses)

    def test_bug4_winrar_repair_extract_never_pollutes_source_dir(self):
        """Bug #4：WinRAREngine.repair_extract 不在源压缩包所在目录创建 _repaired。"""
        src_dir = self.scratch / "user_source_dir"
        src_dir.mkdir()
        src_rar = src_dir / "broken.rar"
        src_rar.write_bytes(b"Rar!\x1a\x07\x00dummy")
        dest_dir = self.scratch / "repair_dest"
        dest_dir.mkdir()

        engine = WinRAREngine(self.fake_exe)
        seen_repair_dirs: list[Path] = []

        def fake_run_command(cmd, timeout, task_id=None, **kwargs):
            # cmd: [exe, "r", "-ibck", "-inul", "-y", "-p-", archive, repaired_dir/]
            rep_dir = Path(cmd[-1])
            seen_repair_dirs.append(rep_dir)
            self.assertNotEqual(rep_dir.parent.resolve(), src_dir.resolve())
            self.assertTrue(rep_dir.name.startswith("sb_repair_"))
            # 模拟生成修复后的包
            (rep_dir / "rebuilt.broken.rar").write_bytes(b"repaired")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(engine, "_run_command", side_effect=fake_run_command), \
             patch.object(engine, "extract", return_value=dest_dir) as mock_extract:
            engine.repair_extract(src_rar, dest_dir)

        mock_extract.assert_called_once()
        # 源目录绝无 _repaired 子目录
        self.assertFalse((src_dir / "_repaired").exists())
        self.assertEqual([p.name for p in src_dir.iterdir()], ["broken.rar"])
        # 系统临时修复目录已在 finally 中回收
        self.assertEqual(len(seen_repair_dirs), 1)
        self.assertFalse(seen_repair_dirs[0].exists())

    def test_bug5_nested_001_and_split_volumes_skip_and_cleanup(self):
        """Bug #5：嵌套层 .001 与分卷次卷跳过 + 首卷解压后清理沙箱内全部同族分卷。"""
        sandbox_dir = self.sandbox_root / "sb_bug5"
        output_dir = sandbox_dir / "output"
        output_dir.mkdir(parents=True)

        # 放一组 .7z.001 / .7z.002 / .7z.003 + 一组 .part1.rar / .part2.rar
        vol_7z_1 = output_dir / "game.7z.001"
        vol_7z_2 = output_dir / "game.7z.002"
        vol_7z_3 = output_dir / "game.7z.003"
        with zipfile.ZipFile(vol_7z_1, "w") as zf:
            zf.writestr("readme_7z.txt", "ok_7z")
        vol_7z_2.write_bytes(b"part2")
        vol_7z_3.write_bytes(b"part3")

        vol_rar_1 = output_dir / "patch.part1.rar"
        vol_rar_2 = output_dir / "patch.part2.rar"
        with zipfile.ZipFile(vol_rar_1, "w") as zf:
            zf.writestr("readme_rar.txt", "ok_rar")
        vol_rar_2.write_bytes(b"rar_part2")

        engine = _FakeZipEngine(self.fake_exe)
        pipeline = ExtractionPipeline(
            engine=engine,
            engine_name="FakeEngine",
            sandbox=SandboxManager(),
            config=self.cfg,
        )
        pipeline._task_counters["t_bug5"] = {"total": 1, "cycle": 1}

        pipeline.extract_nested("t_bug5", sandbox_dir, str(output_dir), [], depth=1)

        extracted_names = [name for name, _ in engine.extract_calls]
        # 仅首卷被解压，次卷全部跳过
        self.assertIn("game.7z.001", extracted_names)
        self.assertIn("patch.part1.rar", extracted_names)
        self.assertNotIn("game.7z.002", extracted_names)
        self.assertNotIn("game.7z.003", extracted_names)
        self.assertNotIn("patch.part2.rar", extracted_names)

        # 沙箱内所有同族分卷均已清理干净，仅留最终业务文件
        remaining = sorted(p.name for p in output_dir.iterdir())
        self.assertEqual(remaining, ["readme_7z.txt", "readme_rar.txt"])

    def test_bug6_terminate_all_abort_all_and_gentle_reload_config(self):
        """Bug #6：BaseEngine.terminate_all()、TaskQueue.abort_all()、reload_config() 不丢弃排队任务。"""
        # 1) BaseEngine.terminate_all() 强杀全部活跃子进程并清空记录
        engine = _FakeZipEngine(self.fake_exe)
        p1, p2 = MagicMock(pid=101), MagicMock(pid=102)
        with engine._lock:
            engine._active_processes["t1"] = p1
            engine._active_processes["t2"] = p2
        engine.terminate_all()
        p1.kill.assert_called_once()
        p2.kill.assert_called_once()
        self.assertEqual(engine._active_processes, {})
        self.assertTrue(engine.is_aborted("t1"))
        self.assertTrue(engine.is_aborted("t2"))

        # 2) TaskQueue.reload_config()：max_workers 不变时不重建池；变更时旧池排队任务仍完成
        self.cfg.set("max_workers", 1)
        tq = TaskQueue()
        tq._engine = _FakeZipEngine(self.fake_exe)
        tq._engine_name = "FakeEngine"
        tq.start()
        try:
            orig_executor = tq._executor
            with patch.object(tq, "_ensure_engine"):
                tq.reload_config()
            # max_workers 未变，线程池实例不变
            self.assertIs(tq._executor, orig_executor)

            # 投递两个阻塞/排队任务，在第二个还在排队时修改 max_workers 并 reload_config
            gate = threading.Event()
            done_ids: list[str] = []
            done_ev = threading.Event()

            def fake_process(tid, sp):
                if tid == "job1":
                    gate.wait(timeout=3.0)
                done_ids.append(tid)
                if len(done_ids) == 2:
                    done_ev.set()
                return True

            with patch.object(tq, "_process_task", side_effect=fake_process), \
                 patch.object(tq, "_ensure_engine"):
                tq.submit_existing("job1", str(self.scratch / "j1.zip"))
                tq.submit_existing("job2", str(self.scratch / "j2.zip"))
                time.sleep(0.05)

                # 修改 max_workers 触发温和替换线程池（绝不 cancel_futures）
                self.cfg.set("max_workers", 2)
                tq.reload_config()
                self.assertIsNot(tq._executor, orig_executor)

                gate.set()
                self.assertTrue(done_ev.wait(timeout=5.0), "旧池中排队的任务必须自然跑完，不能被丢弃")
                self.assertEqual(set(done_ids), {"job1", "job2"})
        finally:
            tq.shutdown(wait=True)

        # 3) TaskQueue.abort_all() 唤醒等待事件、终止引擎并清理沙箱
        tq2 = TaskQueue()
        mock_eng = MagicMock()
        tq2._engine = mock_eng
        tq2._engine_name = "MockEng"
        tq2.start()
        try:
            leftover_sb = self.sandbox_root / "leftover_sb"
            leftover_sb.mkdir()
            (leftover_sb / "tmp.dat").write_bytes(b"123")

            pwd_evt = threading.Event()
            nest_evt = threading.Event()
            with tq2._lock:
                tq2._password_events["tw1"] = pwd_evt
                tq2._nested_confirm_events["tw2"] = nest_evt

            tq2.abort_all()
            self.assertTrue(pwd_evt.is_set())
            self.assertTrue(nest_evt.is_set())
            mock_eng.terminate_all.assert_called_once()
            self.assertFalse(leftover_sb.exists())
        finally:
            tq2.shutdown(wait=True)


# ==================== Phase 1：引擎进度、双计数器与三态暂停 ====================

class TestPhase1EngineProgressAndCounters(unittest.TestCase):
    """Phase 1：BaseEngine._run_command 进度提取与旁路估算 + ExtractionPipeline 双计数器精准计次。"""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="sb_p1_eng_"))
        self.sandbox_root = self.scratch / "sandboxes"
        self.sandbox_root.mkdir()
        self._orig_sandbox_root = sandbox_mod.SANDBOX_ROOT
        sandbox_mod.SANDBOX_ROOT = self.sandbox_root

        self.cfg = ConfigManager()
        self._orig_cfg = self.cfg.get_all()
        self.cfg.set("output_mode", "custom")
        self.out_dir = self.scratch / "delivered"
        self.out_dir.mkdir()
        self.cfg.set("custom_output_path", str(self.out_dir))
        self.cfg.set("min_file_size_mb", 0)
        self.cfg.set("delete_source_after_extract", False)
        self.cfg.set("enable_nested_limit_confirm", False)

        self.fake_exe = self.scratch / "fake.exe"
        self.fake_exe.write_bytes(b"MZ")

    def tearDown(self):
        sandbox_mod.SANDBOX_ROOT = self._orig_sandbox_root
        for k, v in self._orig_cfg.items():
            self.cfg.set(k, v)
        self.cfg.save()
        robust_rmtree(self.scratch)

    def test_run_command_stdout_cr_percentage_streaming(self):
        """BaseEngine._run_command：实时提取含 \\r 覆盖行的 stdout 百分比。"""
        engine = _FakeZipEngine(self.fake_exe)
        reported: list[int] = []

        script = (
            "import sys, time\n"
            "for p in (5, 28, 63, 100):\n"
            "    sys.stdout.write(f'Extracting ... {p}%\\r')\n"
            "    sys.stdout.flush()\n"
            "    time.sleep(0.03)\n"
            "sys.stdout.write('\\nDone\\n')\n"
        )
        res = engine._run_command(
            [sys.executable, "-c", script],
            timeout=10,
            on_progress=lambda p: reported.append(p),
        )
        self.assertEqual(res.returncode, 0)
        self.assertEqual(reported, [5, 28, 63, 100])

    def test_run_command_sidechannel_dir_size_estimation(self):
        """BaseEngine._run_command：无 stdout 百分比时通过沙箱目录体积旁路采样估算进度（上限 99%）。"""
        engine = _FakeZipEngine(self.fake_exe)
        monitor_dir = self.scratch / "mon_out"
        monitor_dir.mkdir()
        reported: list[int] = []

        # 子进程不打印任何百分比，持续向 monitor_dir 写约 100KB 数据（模拟 archive_size=100KB）
        script = (
            "import pathlib, time\n"
            f"d = pathlib.Path({str(monitor_dir)!r})\n"
            "for i in range(4):\n"
            "    (d / f'chunk_{i}.bin').write_bytes(b'A' * 30000)\n"
            "    time.sleep(0.28)\n"
        )
        res = engine._run_command(
            [sys.executable, "-c", script],
            timeout=15,
            on_progress=lambda p: reported.append(p),
            monitor_dest=monitor_dir,
            archive_size=100_000,
        )
        self.assertEqual(res.returncode, 0)
        self.assertTrue(len(reported) >= 1, f"应触发旁路采样进度，实际: {reported}")
        self.assertTrue(any(0 < p <= 99 for p in reported), f"应产生 1~99 的估算进度，实际: {reported}")
        self.assertTrue(all(0 <= p <= 99 for p in reported), f"旁路估算不得提前到达 100%，实际: {reported}")

    def test_dual_counter_ignores_wrong_passwords_and_disguise_rollbacks(self):
        """双计数器精准计次：密码错试多次 + 伪装试探失败回滚均不计次，真实解压 3 层后 total == 3。"""
        # 构造 3 层：
        # 1) outer.zip -> 含 fake_video.mp4 (非压缩包，会试探失败回滚) + inner_pwd.zip (加密)
        #    注意：为触发单一大文件伪装试探，先让层 1 解出 real_disguise.jpg (实为 zip)，
        #    层 2 解出 inner_pwd.zip (需密码 'right_pwd')，
        #    层 3 解出 not_archive.mp4 (真实 MP4，伪装试探失败回滚，不计次)
        work = self.scratch / "build_3layers"
        work.mkdir()

        # 最内层真实非压缩包伪装后缀（触发伪装试探 -> 失败回滚）
        real_mp4 = work / "real_movie.mp4"
        real_mp4.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 128)

        # 层 3：inner_pwd.zip（需密码，前面放 2 个错误密码）
        layer3 = work / "inner_pwd.zip"
        with zipfile.ZipFile(layer3, "w") as zf:
            zf.write(real_mp4, arcname="real_movie.mp4")

        # 层 2：伪装成图片 disguise_pic.jpg（实际为 zip，含 inner_pwd.zip）
        layer2 = work / "disguise_pic.jpg"
        with zipfile.ZipFile(layer2, "w") as zf:
            zf.write(layer3, arcname="inner_pwd.zip")

        # 层 1：outer.zip（含 disguise_pic.jpg）
        outer = work / "outer.zip"
        with zipfile.ZipFile(outer, "w") as zf:
            zf.write(layer2, arcname="disguise_pic.jpg")

        # 配置密码库（两个错误密码 + 一个正确密码）
        pwd_mgr = PasswordManager()
        orig_file = pwd_mgr._file
        orig_cache = [dict(x) for x in pwd_mgr._cache]
        orig_loaded = pwd_mgr._loaded
        tmp_pwd_file = self.scratch / "pwds.json"
        try:
            pwd_mgr._file = tmp_pwd_file
            pwd_mgr._cache = []
            pwd_mgr._loaded = True
            pwd_mgr.add("wrong_1", "[T]", count=10)
            pwd_mgr.add("wrong_2", "[T]", count=5)
            pwd_mgr.add("right_pwd", "[T]", count=1)

            engine = _FakeZipEngine(
                self.fake_exe,
                password_map={"inner_pwd.zip": "right_pwd"},
                progress_steps=[50, 100],
            )
            layer_events: list[tuple[int, int, str]] = []
            pipeline = ExtractionPipeline(
                engine=engine,
                engine_name="FakeEngine",
                sandbox=SandboxManager(),
                password_manager=pwd_mgr,
                config=self.cfg,
                on_layer_progress=lambda tid, pct, cnt, txt: layer_events.append((pct, cnt, txt)),
            )

            ok = pipeline.process("t_cnt3", str(outer))
            self.assertTrue(ok)

            # 确认确实经历了 2 次密码错试 + 1 次伪装试探失败回滚
            self.assertIn(("inner_pwd.zip", "wrong_1"), engine.extract_calls)
            self.assertIn(("inner_pwd.zip", "wrong_2"), engine.extract_calls)
            self.assertIn(("inner_pwd.zip", "right_pwd"), engine.extract_calls)
            self.assertIn(("real_movie.zip", None), engine.extract_calls)

            # 校验 3 层完成后计数精确为 3，且末尾回滚后通过 _restore_layer_progress 恢复为 (100, 3) 而非误加到 4
            completed_milestones = [
                (pct, cnt) for pct, cnt, txt in layer_events if txt.startswith("已完成第")
            ]
            self.assertEqual(completed_milestones, [(100, 1), (100, 2), (100, 3), (100, 3)])
            self.assertEqual(layer_events[-1][0], 100)
            self.assertEqual(layer_events[-1][1], 3)

            # 验证第 k 层开始时推 (0, k-1)，且从未超过 3
            self.assertIn((0, 0), [(p, c) for p, c, _ in layer_events])
            self.assertIn((0, 1), [(p, c) for p, c, _ in layer_events])
            self.assertIn((0, 2), [(p, c) for p, c, _ in layer_events])
            self.assertEqual(max(c for _, c, _ in layer_events), 3)

            # 验证最终交付物完整保留了回滚后的 real_movie.mp4
            delivered = self.out_dir / "outer"
            self.assertTrue((delivered / "real_movie.mp4").is_file())
        finally:
            pwd_mgr._file = orig_file
            pwd_mgr._cache = orig_cache
            pwd_mgr._loaded = orig_loaded


class TestPhase1NestedLimitThreeStates(unittest.TestCase):
    """Phase 1：真实多线程 TaskQueue + Mock 嵌套引擎，验证 nested_confirm_limit=2 三态全覆盖。"""

    def setUp(self):
        self.scratch = Path(tempfile.mkdtemp(prefix="sb_p1_limit_"))
        self.sandbox_root = self.scratch / "sandboxes"
        self.sandbox_root.mkdir()
        self._orig_sandbox_root = sandbox_mod.SANDBOX_ROOT
        sandbox_mod.SANDBOX_ROOT = self.sandbox_root

        self.cfg = ConfigManager()
        self._orig_cfg = self.cfg.get_all()
        self.out_dir = self.scratch / "delivered"
        self.out_dir.mkdir()
        self.cfg.set("output_mode", "custom")
        self.cfg.set("custom_output_path", str(self.out_dir))
        self.cfg.set("min_file_size_mb", 0)
        self.cfg.set("delete_source_after_extract", False)
        self.cfg.set("enable_nested_limit_confirm", True)
        self.cfg.set("nested_confirm_limit", 2)
        self.cfg.set("max_workers", 2)

        self.fake_exe = self.scratch / "fake.exe"
        self.fake_exe.write_bytes(b"MZ")

    def tearDown(self):
        sandbox_mod.SANDBOX_ROOT = self._orig_sandbox_root
        for k, v in self._orig_cfg.items():
            self.cfg.set(k, v)
        self.cfg.save()
        robust_rmtree(self.scratch)

    def _run_threaded_scenario(self, layers: int, choices: list[str]):
        src = _create_nested_zip_chain(self.scratch / f"src_{layers}_{choices[0]}", layers)
        tq = TaskQueue()
        tq._engine = _FakeZipEngine(self.fake_exe)
        tq._engine_name = "FakeEngine"
        tq.start()

        triggered_counts: list[int] = []
        status_history: list[tuple[str, str, str]] = []
        done_event = threading.Event()
        choice_iter = iter(choices)

        def on_limit(tid, fname, total_cnt):
            triggered_counts.append(total_cnt)
            choice = next(choice_iter, "cancel")
            # 模拟异步回传用户选择（避免直接在同线程同步重入）
            threading.Thread(
                target=lambda: (time.sleep(0.02), tq.supply_nested_confirm(tid, choice)),
                daemon=True,
            ).start()

        def on_status(tid, fn, st, eng, rmk):
            status_history.append((st, eng, rmk))
            if st in ("成功", "失败", "已中止"):
                done_event.set()

        # 使用 DirectConnection 确保在无主线程事件循环的后台触发也能实时收到回调
        tq.nested_limit_reached.connect(on_limit, Qt.ConnectionType.DirectConnection)
        tq.task_status_changed.connect(on_status, Qt.ConnectionType.DirectConnection)

        try:
            task_id = tq.submit(str(src))
            self.assertIsNotNone(task_id)
            self.assertTrue(done_event.wait(timeout=10.0), f"任务未在超时内完成，历史状态: {status_history}")
        finally:
            tq.shutdown(wait=True)

        return triggered_counts, status_history

    def test_scenario_a_continue_resets_cycle_and_retriggers_at_total_4(self):
        """场景 A：5 层嵌套 + limit=2，两次选 continue → 分别在 total==2 与 total==4 触发暂停，最终成功交付。"""
        (self.scratch / "src_5_continue").mkdir()
        triggered, statuses = self._run_threaded_scenario(layers=5, choices=["continue", "continue"])

        self.assertEqual(triggered, [2, 4], f"应在第 2 次和第 4 次触发确认，实际: {triggered}")
        self.assertIn("待确认继续", [s[0] for s in statuses])
        self.assertEqual(statuses[-1][0], "成功")

        delivered = Path(statuses[-1][2])
        self.assertTrue((delivered / "final_payload.txt").is_file())
        self.assertEqual((delivered / "final_payload.txt").read_text(encoding="utf-8"), "shellbreaker payload ok")

    def test_scenario_b_stop_keep_delivers_current_layer_and_succeeds(self):
        """场景 B：5 层嵌套 + limit=2，选 stop_keep → 停止下钻并交付当前层（含 layer_3.zip），终态成功。"""
        (self.scratch / "src_5_stop_keep").mkdir()
        triggered, statuses = self._run_threaded_scenario(layers=5, choices=["stop_keep"])

        self.assertEqual(triggered, [2])
        self.assertEqual(statuses[-1][0], "成功")

        delivered = Path(statuses[-1][2])
        # 已解压 2 层（layer_1.zip -> layer_2.zip -> layer_3.zip），交付物中应完整保留 layer_3.zip
        self.assertTrue((delivered / "layer_3.zip").is_file())
        self.assertFalse((delivered / "final_payload.txt").exists())

    def test_scenario_c_cancel_aborts_and_destroys_sandbox(self):
        """场景 C：5 层嵌套 + limit=2，选 cancel → 抛 ExtractionAbortedError，沙箱彻底销毁，终态已中止。"""
        (self.scratch / "src_5_cancel").mkdir()
        triggered, statuses = self._run_threaded_scenario(layers=5, choices=["cancel"])

        self.assertEqual(triggered, [2])
        self.assertEqual(statuses[-1][0], "已中止")
        # 沙箱应已彻底回收，无残留目录
        self.assertEqual(list(self.sandbox_root.iterdir()), [])
        # 不应产出目标交付目录
        self.assertEqual(list(self.out_dir.iterdir()), [])


# ==================== Phase 1：UI 组件与页面联调 ====================

class TestPhase1UIComponentsAndIntegration(unittest.TestCase):
    """Phase 1：ExtractionProgressWidget、TasksPage、StatusPillDelegate、SettingsPage、MainWindow 联调。"""

    def setUp(self):
        self.cfg = ConfigManager()
        self._orig_cfg = self.cfg.get_all()

    def tearDown(self):
        for k, v in self._orig_cfg.items():
            self.cfg.set(k, v)
        self.cfg.save()

    def test_extraction_progress_widget_counts_collapse_animation_and_terminal(self):
        """ExtractionProgressWidget：0/3/5/6/12 离屏渲染、折叠阈值、不确定动画启停与终态显示。"""
        widget = ExtractionProgressWidget(max_green_cells=5)
        widget.resize(214, 36)

        try:
            for count, expected_collapsed in [(0, False), (3, False), (5, False), (6, True), (12, True)]:
                widget.update_state(45, count, f"第 {count} 层")
                self.assertEqual(widget.completed_count, count)
                self.assertEqual(widget.is_collapsed, expected_collapsed)
                self.assertFalse(widget.is_indeterminate)
                # 触发真实离屏 paintEvent，确保不崩溃
                pix = widget.grab()
                self.assertFalse(pix.isNull())

            # 动态调整 max_green_cells=12 -> 此时 count=12 不再折叠
            widget.set_max_green_cells(12)
            self.assertFalse(widget.is_collapsed)
            self.assertFalse(widget.grab().isNull())

            # 不确定模式：启动动画定时器，切回确定态或 stop_animation 后停止
            widget.update_state(-1, 2, "正在解压...")
            self.assertTrue(widget.is_indeterminate)
            self.assertTrue(widget._indeterminate_timer.isActive())
            self.assertFalse(widget.grab().isNull())

            widget.stop_animation()
            self.assertFalse(widget._indeterminate_timer.isActive())

            # 终态展示校验
            for st, expected_label in [
                ("成功", "100%"),
                ("失败", "失败"),
                ("已中止", "中止"),
                ("已跳过(文件过小)", "跳过"),
            ]:
                widget.set_terminal_state(st)
                self.assertEqual(widget.terminal_state, st)
                self.assertEqual(widget._right_label_text(), expected_label)
                self.assertFalse(widget._indeterminate_timer.isActive())
                self.assertFalse(widget.grab().isNull())
        finally:
            widget.close()

    def test_tasks_page_5_columns_and_status_pill_pending_confirm(self):
        """TasksPage 5 列结构常量 + StatusPillDelegate 对 '待确认继续' 的配色与离屏绘制。"""
        page = TasksPage()
        try:
            self.assertEqual(TasksPage.COL_FILENAME, 0)
            self.assertEqual(TasksPage.COL_STATUS, 1)
            self.assertEqual(TasksPage.COL_PROGRESS, 2)
            self.assertEqual(TasksPage.COL_ENGINE, 3)
            self.assertEqual(TasksPage.COL_REMARK, 4)

            self.assertEqual(page.table.columnCount(), 5)
            headers = [page.table.horizontalHeaderItem(i).text() for i in range(5)]
            self.assertEqual(headers, HEADERS)
            self.assertEqual(headers[TasksPage.COL_PROGRESS], "解压进度")

            # StatusPillDelegate 对 "待确认继续" 使用 warning 主题色
            theme = get_current_theme()
            fg, bg = status_palette(theme, "待确认继续")
            self.assertEqual((fg, bg), (theme.warning, theme.warning_bg))

            # 离屏绘制 "待确认继续" 胶囊不崩溃
            delegate = StatusPillDelegate(page.table)
            page.table.insertRow(0)
            from PySide6.QtWidgets import QTableWidgetItem
            page.table.setItem(0, TasksPage.COL_STATUS, QTableWidgetItem("待确认继续"))
            idx = page.table.model().index(0, TasksPage.COL_STATUS)

            pix = QPixmap(160, 44)
            pix.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pix)
            opt = QStyleOptionViewItem()
            opt.rect = QRect(0, 0, 160, 44)
            opt.font = page.table.font()
            delegate.paint(painter, opt, idx)
            painter.end()
            self.assertFalse(pix.isNull())
        finally:
            page.close()

    def test_settings_page_advanced_nested_limit_and_min_file_size(self):
        """SettingsPage：enable_nested_limit_confirm 与 nested_confirm_limit 联动及持久化、统一 min_file_size_mb (含 0 不限制) 持久化。"""
        self.cfg.set("enable_nested_limit_confirm", True)
        self.cfg.set("nested_confirm_limit", 10)
        self.cfg.set("min_file_size_mb", 0)
        self.cfg.save()

        page = SettingsPage()
        try:
            self.assertFalse(hasattr(page, "spin_disguise_min_size"))
            self.assertTrue(page.cb_nested_limit_confirm.isChecked())
            self.assertTrue(page.spin_nested_confirm_limit.isEnabled())
            self.assertEqual(page.spin_nested_confirm_limit.value(), 10)
            # 回归断言：min_file_size_mb=0 不能被 reload_from_config 误吞为 50
            self.assertEqual(page.spin_min_size.minimum(), 0)
            self.assertEqual(page.spin_min_size.value(), 0)

            # 修改阈值与最小文件大小并校验即时持久化
            page.spin_nested_confirm_limit.setValue(18)
            self.assertEqual(self.cfg.get("nested_confirm_limit"), 18)

            page.spin_min_size.setValue(25)
            self.assertEqual(self.cfg.get("min_file_size_mb"), 25)

            page.spin_min_size.setValue(0)
            self.assertEqual(self.cfg.get("min_file_size_mb"), 0)

            # 关闭开关 -> 阈值输入框同步禁用并持久化 False
            page.cb_nested_limit_confirm.setChecked(False)
            self.assertFalse(page.spin_nested_confirm_limit.isEnabled())
            self.assertFalse(self.cfg.get("enable_nested_limit_confirm"))

            # 重新加载新 SettingsPage 校验持久态回显 (含 0 值回显)
            page2 = SettingsPage()
            try:
                self.assertFalse(page2.cb_nested_limit_confirm.isChecked())
                self.assertFalse(page2.spin_nested_confirm_limit.isEnabled())
                self.assertEqual(page2.spin_nested_confirm_limit.value(), 18)
                self.assertEqual(page2.spin_min_size.value(), 0)

                page2.spin_min_size.setValue(25)
                self.assertEqual(self.cfg.get("min_file_size_mb"), 25)
            finally:
                page2.close()
        finally:
            page.close()

    def test_nested_limit_confirm_dialog_three_choices(self):
        """NestedLimitConfirmDialog：默认 stop_keep 及三个按钮分别返回 continue / stop_keep / cancel。"""
        dlg_default = NestedLimitConfirmDialog("demo.zip", 10)
        self.assertEqual(dlg_default.get_choice(), "stop_keep")
        dlg_default.close()

        dlg_cont = NestedLimitConfirmDialog("demo.zip", 10)
        dlg_cont.btn_continue.click()
        self.assertEqual(dlg_cont.get_choice(), "continue")
        dlg_cont.close()

        dlg_stop = NestedLimitConfirmDialog("demo.zip", 10)
        dlg_stop.btn_stop_keep.click()
        self.assertEqual(dlg_stop.get_choice(), "stop_keep")
        dlg_stop.close()

        dlg_cancel = NestedLimitConfirmDialog("demo.zip", 10)
        dlg_cancel.btn_cancel_task.click()
        self.assertEqual(dlg_cancel.get_choice(), "cancel")
        dlg_cancel.close()

    def test_main_window_nested_limit_signal_wakes_worker_without_deadlock_and_max_green_cells(self):
        """MainWindow 信号联动：nested_limit_reached 弹窗选择唤醒 Worker 不死锁 + max_green_cells 动态生效。"""
        self.cfg.set("max_green_cells", 4)
        self.cfg.save()

        win = MainWindow()
        try:
            # 1) 验证新行进度组件读取 max_green_cells=4，且配置变化后实时同步
            win._on_task_status_changed("t_mw", "chain.zip", "正在提取", "Bandizip", "")
            pw = win._progress_widgets["t_mw"]
            self.assertEqual(pw.max_green_cells, 4)

            self.cfg.set("max_green_cells", 7)
            win._on_settings_page_changed("max_green_cells")
            self.assertEqual(pw.max_green_cells, 7)

            # 2) 验证真实工作线程挂起在 _wait_for_nested_confirm，由 MainWindow 槽函数唤醒且无死锁
            worker_result: list[str] = []

            def worker_fn():
                res = win._task_queue._wait_for_nested_confirm("t_mw", "layer_3.zip", 2)
                worker_result.append(res)

            # Mock 弹窗 exec：在被调用时自动模拟点击 "继续解压"
            orig_exec = NestedLimitConfirmDialog.exec

            def fake_exec(dlg_self):
                dlg_self._on_continue()
                return 1

            with patch.object(NestedLimitConfirmDialog, "exec", fake_exec):
                th = threading.Thread(target=worker_fn, daemon=True)
                th.start()

                # 驱动 Qt 事件循环，让队列信号投递到主线程触发弹窗并调用 supply_nested_confirm
                deadline = time.monotonic() + 5.0
                while th.is_alive() and time.monotonic() < deadline:
                    _pump_qt_events(25)
                th.join(timeout=2.0)

            self.assertFalse(th.is_alive(), "Worker 线程应被 MainWindow 唤醒，绝不可死锁")
            self.assertEqual(worker_result, ["continue"])
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
