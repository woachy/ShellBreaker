"""
Phase 3 (Batch 3 / P2) 全量集成与回归测试套件：
1. 配置扩展（19 键）与三档源文件删除模式双向兼容
2. SevenZipEngine（-bsp1 实时进度、错误分类、-slt 加密头/最小成员快速验密、兜底发现与真实 7z 端到端）
3. 图种魔数偏移嗅探（JPEG+ZIP / PNG+RAR5 / 跨 1MB 边界 7z / MP4 防误报 / 白名单）与管线剥离提取
4. 分卷连续性预检、TaskQueue 入队拦截、StatusPill 与 MainWindow 终态及备注保护
5. 删除三模式（keep / recycle_bin / permanent）与推广垃圾文件黑名单过滤（含安全底线）
6. 系统集成（单实例 IPC 中文路径转发 / WatchFolderService 稳定性检测）
7. 动态主题切换（ensure_theme 优先级与 set_theme 广播）、SettingsPage 全量 P2 控件、原生对话框与 ContentDialog 离屏渲染、MainWindow 联动与 CLI 解析

全部用例支持 QT_QPA_PLATFORM=offscreen 离屏运行，严禁污染真实 data/config.json / data/passwords.json。
"""
from __future__ import annotations

import copy
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
import zipfile
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QRect, QTimer, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox, QStyleOptionViewItem

_APP = QApplication.instance() or QApplication(sys.argv)

import core.engine as engine_mod
import core.sandbox as sandbox_mod
import data.config_manager as cfg_mod
import main as main_mod
import ui.theme.theme_manager as tm_mod
from core.engine import (
    BaseEngine,
    EngineType,
    SevenZipEngine,
    create_engine,
    discover_engine,
    get_engine_display_name,
)
from core.exceptions import (
    ExtractionAbortedError,
    ExtractionFailedError,
    PasswordRequiredError,
    WrongPasswordError,
)
from core.pipeline import ExtractionPipeline
from core.sandbox import (
    SandboxManager,
    check_volume_completeness,
    detect_archive_type,
    detect_embedded_archive,
    robust_rmtree,
)
from core.system_integration import (
    SingleInstanceBridge,
    WatchFolderService,
)
from core.task_queue import TaskQueue
from data.config_manager import ConfigManager, DEFAULT_CONFIG
from data.password_manager import PasswordManager
from ui.main_window import MainWindow, _is_terminal_status
from ui.pages.settings_page import SettingsPage, parse_ad_filter_patterns
from ui.pages.tasks_page import TasksPage
from ui.password_dialog import PasswordDialog
from ui.settings_dialog import SettingsDialog
from ui.theme.theme_manager import (
    ensure_theme,
    get_current_theme,
    get_theme_notifier,
    set_theme,
)
from ui.theme.tokens import tokens_for
from ui.widgets.content_dialog import ContentDialog

DARK_TOKENS = tokens_for("dark")
LIGHT_TOKENS = tokens_for("light")
from ui.widgets.status_pill import StatusPillDelegate, status_palette


# ==================== 测试辅助工具 ====================


def _pump_events(duration_ms: int = 40) -> None:
    """离屏安全泵出 Qt 事件循环。"""
    app = QApplication.instance()
    if app is None:
        return
    loop = QEventLoop()
    QTimer.singleShot(max(1, int(duration_ms)), loop.quit)
    loop.exec()
    app.processEvents()


def _wait_until(predicate, timeout_s: float = 1.5, step_ms: int = 25) -> bool:
    """在超时前循环泵出 Qt 事件直到条件成立。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        _pump_events(step_ms)
    return bool(predicate())


class _IsolatedConfigMixin:
    """隔离 ConfigManager、PasswordManager 与 SANDBOX_ROOT，严禁污染真实磁盘配置。"""

    def _setup_isolated_env(self, prefix: str = "sb_p3_") -> None:
        self.scratch = Path(tempfile.mkdtemp(prefix=prefix))
        self.sandbox_root = self.scratch / "sandboxes"
        self.sandbox_root.mkdir(parents=True, exist_ok=True)
        self._orig_sandbox_root = sandbox_mod.SANDBOX_ROOT
        sandbox_mod.SANDBOX_ROOT = self.sandbox_root

        self.cfg = ConfigManager()
        self._orig_cfg_path = cfg_mod.CONFIG_PATH
        self._orig_cfg_data = copy.deepcopy(self.cfg._data)
        self._orig_cfg_loaded = self.cfg._loaded

        self.tmp_cfg_path = self.scratch / "config.json"
        cfg_mod.CONFIG_PATH = self.tmp_cfg_path
        with self.cfg._lock:
            self.cfg._data = copy.deepcopy(DEFAULT_CONFIG)
            self.cfg._loaded = True
            self.cfg.save()

        self.pwd_mgr = PasswordManager()
        self._orig_pwd_file = self.pwd_mgr._file
        self._orig_pwd_cache = copy.deepcopy(self.pwd_mgr._cache)
        self._orig_pwd_loaded = self.pwd_mgr._loaded

        self.tmp_pwd_path = self.scratch / "passwords.json"
        self.pwd_mgr._file = self.tmp_pwd_path
        self.pwd_mgr._cache = []
        self.pwd_mgr._loaded = True

    def _teardown_isolated_env(self) -> None:
        sandbox_mod.SANDBOX_ROOT = self._orig_sandbox_root

        with self.cfg._lock:
            cfg_mod.CONFIG_PATH = self._orig_cfg_path
            self.cfg._data = self._orig_cfg_data
            self.cfg._loaded = self._orig_cfg_loaded

        self.pwd_mgr._file = self._orig_pwd_file
        self.pwd_mgr._cache = self._orig_pwd_cache
        self.pwd_mgr._loaded = self._orig_pwd_loaded

        robust_rmtree(self.scratch)


class _FakeZipOnlyOffsetZeroEngine(BaseEngine):
    """仅能解压偏移 0 标准 ZIP 的模拟引擎（用于验证图种偏移剥离链路）。"""

    def __init__(self, exe_path: Path):
        super().__init__(exe_path)
        self.extracted_files: list[str] = []

    def extract(
        self,
        archive: str | Path,
        dest: str | Path,
        password=None,
        timeout=300,
        task_id=None,
        on_progress=None,
    ) -> Path:
        p = Path(archive)
        dest_p = Path(dest)
        dest_p.mkdir(parents=True, exist_ok=True)
        self.extracted_files.append(p.name)

        with open(p, "rb") as f:
            head = f.read(4)
        # 严格要求偏移 0 必须是 PK\x03\x04，模拟无法跳过图种前缀的引擎
        if head != b"PK\x03\x04":
            raise ExtractionFailedError(str(p), 2, "not an offset-0 zip archive")

        try:
            with zipfile.ZipFile(p, "r") as zf:
                zf.extractall(dest_p)
        except Exception as e:
            raise ExtractionFailedError(str(p), 2, str(e))

        if on_progress is not None:
            on_progress(100)
        return dest_p


# ==================== 1. 配置扩展与三档删除双向兼容 ====================


class TestPhase3ConfigAndDeleteModeCompat(_IsolatedConfigMixin, unittest.TestCase):
    """验证 DEFAULT_CONFIG 19 键一致性、旧配置迁移以及 delete_source_mode 双向同步。"""

    def setUp(self):
        self._setup_isolated_env("sb_p3_cfg_")

    def tearDown(self):
        self._teardown_isolated_env()

    def test_default_config_keys_match_example_json(self):
        self.assertEqual(len(DEFAULT_CONFIG), 19)
        self.assertNotIn("disguise_min_size_mb", DEFAULT_CONFIG)
        self.assertEqual(DEFAULT_CONFIG["min_file_size_mb"], 50)

        example_path = PROJECT_ROOT / "data" / "config.example.json"
        example_data = json.loads(example_path.read_text(encoding="utf-8"))
        self.assertEqual(set(example_data.keys()), set(DEFAULT_CONFIG.keys()))
        self.assertEqual(example_data, DEFAULT_CONFIG)

        self.assertEqual(DEFAULT_CONFIG["sevenzip_path"], "")
        self.assertEqual(DEFAULT_CONFIG["delete_source_mode"], "keep")
        self.assertIs(DEFAULT_CONFIG["enable_ad_filter"], True)
        self.assertIsInstance(DEFAULT_CONFIG["ad_filter_patterns"], list)
        self.assertIn("*.url", DEFAULT_CONFIG["ad_filter_patterns"])
        self.assertIs(DEFAULT_CONFIG["enable_watch_folder"], False)
        self.assertEqual(DEFAULT_CONFIG["watch_folder_path"], "")
        self.assertEqual(DEFAULT_CONFIG["theme"], "light")

    def test_load_legacy_config_migration(self):
        # 场景 1：旧配置仅有 delete_source_after_extract=True，缺失全部 P2 键
        legacy = {
            "default_engine": "winrar",
            "max_workers": 4,
            "delete_source_after_extract": True,
        }
        self.tmp_cfg_path.write_text(json.dumps(legacy), encoding="utf-8")
        self.cfg.load()

        self.assertEqual(self.cfg.get("delete_source_mode"), "recycle_bin")
        self.assertIs(self.cfg.get("delete_source_after_extract"), True)
        self.assertEqual(self.cfg.get("default_engine"), "winrar")
        self.assertEqual(self.cfg.get("theme"), "light")
        self.assertEqual(self.cfg.get("sevenzip_path"), "")
        self.assertIs(self.cfg.get("enable_ad_filter"), True)
        self.assertEqual(self.cfg.get("ad_filter_patterns"), DEFAULT_CONFIG["ad_filter_patterns"])

        # 场景 2：旧配置仅有 delete_source_after_extract=False
        self.tmp_cfg_path.write_text(
            json.dumps({"delete_source_after_extract": False}), encoding="utf-8"
        )
        self.cfg.load()
        self.assertEqual(self.cfg.get("delete_source_mode"), "keep")
        self.assertIs(self.cfg.get("delete_source_after_extract"), False)

        # 场景 3：新配置显式 permanent，旧布尔键被同步纠正为 True
        self.tmp_cfg_path.write_text(
            json.dumps({"delete_source_mode": "permanent", "delete_source_after_extract": False}),
            encoding="utf-8",
        )
        self.cfg.load()
        self.assertEqual(self.cfg.get("delete_source_mode"), "permanent")
        self.assertIs(self.cfg.get("delete_source_after_extract"), True)

    def test_set_and_update_bidirectional_sync(self):
        # set("delete_source_mode", "permanent") -> 旧键 True
        self.cfg.set("delete_source_mode", "permanent")
        self.assertIs(self.cfg.get("delete_source_after_extract"), True)

        # 处于 permanent 时再 set("delete_source_after_extract", True) 保持 permanent 不被降级
        self.cfg.set("delete_source_after_extract", True)
        self.assertEqual(self.cfg.get("delete_source_mode"), "permanent")

        # 旧键置 False -> 同步为 keep
        self.cfg.set("delete_source_after_extract", False)
        self.assertEqual(self.cfg.get("delete_source_mode"), "keep")

        # keep 状态下旧键置 True -> 同步为 recycle_bin
        self.cfg.set("delete_source_after_extract", True)
        self.assertEqual(self.cfg.get("delete_source_mode"), "recycle_bin")

        # 新键置 keep -> 旧键同步 False
        self.cfg.set("delete_source_mode", "keep")
        self.assertIs(self.cfg.get("delete_source_after_extract"), False)

        # update() 批量同步（delete_source_mode 优先于旧键）
        self.cfg.update(
            {
                "theme": "dark",
                "delete_source_mode": "permanent",
                "delete_source_after_extract": False,
            }
        )
        self.assertEqual(self.cfg.get("theme"), "dark")
        self.assertEqual(self.cfg.get("delete_source_mode"), "permanent")
        self.assertIs(self.cfg.get("delete_source_after_extract"), True)

        self.cfg.update({"delete_source_after_extract": False})
        self.assertEqual(self.cfg.get("delete_source_mode"), "keep")
        self.assertIs(self.cfg.get("delete_source_after_extract"), False)


# ==================== 2. SevenZipEngine 与内置 7-Zip 自动发现 ====================


class TestPhase3SevenZipEngineAndDiscovery(_IsolatedConfigMixin, unittest.TestCase):
    """验证 SevenZipEngine 单元测试、discover_engine 三引擎发现与真实 7z 端到端。"""

    def setUp(self):
        self._setup_isolated_env("sb_p3_7z_")
        self.fake_7z = self.scratch / "7z.exe"
        self.fake_7z.write_bytes(b"MZ")
        self.dummy_archive = self.scratch / "sample.7z"
        self.dummy_archive.write_bytes(b"\x37\x7a\xbc\xaf\x27\x1c" + b"\x00" * 64)

    def tearDown(self):
        self._teardown_isolated_env()

    def test_sevenzip_extract_bsp1_progress_and_error_classification(self):
        engine = SevenZipEngine(self.fake_7z)
        dest = self.scratch / "out_ok"
        progress_seen: list[int] = []
        captured_cmds: list[list[str]] = []

        def fake_run_ok(cmd, timeout, task_id=None, on_progress=None, **kwargs):
            captured_cmds.append(list(cmd))
            if on_progress:
                on_progress(25)
                on_progress(80)
            return subprocess.CompletedProcess(cmd, 0, stdout="Everything is Ok\n", stderr="")

        with patch.object(engine, "_run_command", side_effect=fake_run_ok):
            ret = engine.extract(
                self.dummy_archive, dest, password="secret", on_progress=progress_seen.append
            )

        self.assertEqual(ret, dest.resolve())
        self.assertEqual(progress_seen, [25, 80, 100])
        cmd = captured_cmds[0]
        self.assertIn("x", cmd)
        self.assertIn("-y", cmd)
        self.assertIn("-bsp1", cmd)
        self.assertIn("-psecret", cmd)
        self.assertTrue(any(arg.startswith("-o") for arg in cmd))

        # 密码错 -> WrongPasswordError
        wrong_res = subprocess.CompletedProcess(
            ["7z"], 2, stdout="", stderr="ERROR: Wrong password : file.txt"
        )
        with patch.object(engine, "_run_command", return_value=wrong_res):
            with self.assertRaises(WrongPasswordError):
                engine.extract(self.dummy_archive, self.scratch / "out_wp", password="bad")

        # 需密码 -> PasswordRequiredError
        need_res = subprocess.CompletedProcess(
            ["7z"], 2, stdout="", stderr="ERROR: Can not open encrypted archive. Wrong password?"
        )
        with patch.object(engine, "_run_command", return_value=need_res):
            with self.assertRaises(PasswordRequiredError):
                engine.extract(self.dummy_archive, self.scratch / "out_np", password=None)

        # 非致命警告 returncode=1 且已产出有效文件 -> 容忍成功
        dest_warn = self.scratch / "out_warn"

        def fake_run_warn(cmd, timeout, **kwargs):
            dest_warn.mkdir(parents=True, exist_ok=True)
            (dest_warn / "ok.txt").write_text("partial ok", encoding="utf-8")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="WARNING: minor issue")

        with patch.object(engine, "_run_command", side_effect=fake_run_warn):
            self.assertEqual(engine.extract(self.dummy_archive, dest_warn), dest_warn.resolve())

        # returncode=1 但目录为空 -> ExtractionFailedError
        dest_warn_empty = self.scratch / "out_warn_empty"
        warn_empty_res = subprocess.CompletedProcess(["7z"], 1, stdout="", stderr="WARNING: nothing extracted")
        with patch.object(engine, "_run_command", return_value=warn_empty_res):
            with self.assertRaises(ExtractionFailedError):
                engine.extract(self.dummy_archive, dest_warn_empty)

        # 坏包 returncode=2 -> ExtractionFailedError
        corrupt_res = subprocess.CompletedProcess(["7z"], 2, stdout="", stderr="ERROR: Headers Error")
        with patch.object(engine, "_run_command", return_value=corrupt_res):
            with self.assertRaises(ExtractionFailedError):
                engine.extract(self.dummy_archive, self.scratch / "out_corrupt")

    def test_sevenzip_test_password_header_encrypted_targeted_fallback_cache_and_abort(self):
        # A. 加密文件头 -> l -slt 验密 + 探测缓存
        engine_hdr = SevenZipEngine(self.fake_7z)
        calls_hdr: list[list[str]] = []

        def fake_hdr(cmd, timeout, task_id=None, **kwargs):
            calls_hdr.append(list(cmd))
            if cmd[1] == "l" and "-p" in cmd:
                return subprocess.CompletedProcess(
                    cmd, 2, stdout="", stderr="ERROR: Can not open encrypted archive. Wrong password?"
                )
            if cmd[1] == "l" and "-pgood" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="----------\nPath = a.txt\n", stderr="")
            return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="ERROR: Wrong password")

        with patch.object(engine_hdr, "_run_command", side_effect=fake_hdr):
            self.assertFalse(engine_hdr.test_password(self.dummy_archive, "wrong"))
            self.assertTrue(engine_hdr.test_password(self.dummy_archive, "good"))

        # 首次探测 1 次 + 两次 l -slt 验密 = 共 3 次（第二次复用探测缓存，未重复无密探测）
        self.assertEqual(len(calls_hdr), 3)
        self.assertEqual(calls_hdr[0][1:4], ["l", "-slt", "-p"])
        self.assertEqual(calls_hdr[1][1:4], ["l", "-slt", "-pwrong"])
        self.assertEqual(calls_hdr[2][1:4], ["l", "-slt", "-pgood"])

        # B. 非加密头 -> 解析最小非空成员定向 t，定向非密码失败自动降级整包 t
        engine_mem = SevenZipEngine(self.fake_7z)
        slt_stdout = (
            "Path = sample.7z\n"
            "Type = 7z\n"
            "----------\n"
            "Path = folder_only\n"
            "Folder = +\n"
            "Size = 0\n"
            "\n"
            "Path = empty.txt\n"
            "Folder = -\n"
            "Size = 0\n"
            "\n"
            "Path = big_video.bin\n"
            "Folder = -\n"
            "Size = 999999\n"
            "\n"
            "Path = docs/small.txt\n"
            "Folder = -\n"
            "Size = 42\n"
        )
        calls_mem: list[list[str]] = []

        def fake_mem(cmd, timeout, task_id=None, **kwargs):
            calls_mem.append(list(cmd))
            if cmd[1] == "l":
                return subprocess.CompletedProcess(cmd, 0, stdout=slt_stdout, stderr="")
            if cmd[1] == "t" and cmd[-1] == "docs/small.txt":
                if "-pwrong" in cmd:
                    return subprocess.CompletedProcess(
                        cmd, 2, stdout="", stderr="ERROR: Data Error in encrypted file. Wrong password?"
                    )
                if "-pfallback_ok" in cmd:
                    return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="ERROR: cannot find wildcard")
                return subprocess.CompletedProcess(cmd, 0, stdout="Everything is Ok", stderr="")
            # 整包降级 t
            if cmd[1] == "t" and "-pfallback_ok" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="Everything is Ok", stderr="")
            return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="ERROR: CRC Failed")

        with patch.object(engine_mem, "_run_command", side_effect=fake_mem):
            self.assertFalse(engine_mem.test_password(self.dummy_archive, "wrong"))
            self.assertTrue(engine_mem.test_password(self.dummy_archive, "right"))
            self.assertTrue(engine_mem.test_password(self.dummy_archive, "fallback_ok"))

        # 确认只探测一次，最小成员选为 docs/small.txt，并在定向失败后降级执行整包 t
        probe_cmds = [c for c in calls_mem if c[1] == "l"]
        self.assertEqual(len(probe_cmds), 1)
        self.assertEqual(calls_mem[1][-1], "docs/small.txt")
        self.assertEqual(calls_mem[2][-1], "docs/small.txt")
        self.assertEqual(calls_mem[3][-1], "docs/small.txt")
        self.assertEqual(calls_mem[4][1], "t")
        self.assertEqual(calls_mem[4][-1], str(self.dummy_archive.resolve()))

        # C. 已中止任务立即抛出 ExtractionAbortedError
        engine_mem.terminate_process("tid_abort")
        with self.assertRaises(ExtractionAbortedError):
            engine_mem.test_password(self.dummy_archive, "right", task_id="tid_abort")

    def test_discover_engine_sevenzip_preference_manual_path_and_bundled_fallback(self):
        # 1) 偏好 7zip + 手动 sevenzip_path
        self.cfg.set("default_engine", "7zip")
        self.cfg.set("sevenzip_path", str(self.fake_7z))
        etype, exe = discover_engine(self.cfg)
        self.assertEqual(etype, EngineType.SEVENZIP)
        self.assertEqual(exe, str(self.fake_7z))
        inst = create_engine(exe, etype)
        self.assertIsInstance(inst, SevenZipEngine)
        self.assertEqual(get_engine_display_name(etype), "7-Zip")

        # 2) Bandizip / WinRAR 均缺失时自动兜底内置 assets/bin/7z.exe
        self.cfg.set("default_engine", "bandizip")
        self.cfg.set("bandizip_path", "")
        self.cfg.set("winrar_path", "")
        self.cfg.set("sevenzip_path", "")

        bundled_real = PROJECT_ROOT / "assets" / "bin" / "7z.exe"
        expected_bundled = str(bundled_real.resolve()) if bundled_real.is_file() else str(self.fake_7z)

        with patch.object(engine_mod, "_find_bandizip_cli", return_value=None), \
             patch.object(engine_mod, "_find_winrar_cli", return_value=None), \
             patch.object(engine_mod, "SEVENZIP_PATHS", []), \
             patch.object(engine_mod.shutil, "which", return_value=None):
            if bundled_real.is_file():
                etype2, exe2 = discover_engine(self.cfg)
            else:
                with patch.object(engine_mod, "_find_bundled_sevenzip", return_value=expected_bundled):
                    etype2, exe2 = discover_engine(self.cfg)

        self.assertEqual(etype2, EngineType.SEVENZIP)
        self.assertEqual(Path(exe2).resolve(), Path(expected_bundled).resolve())

    def test_sevenzip_real_extract_end_to_end(self):
        bundled_7z = PROJECT_ROOT / "assets" / "bin" / "7z.exe"
        if not bundled_7z.is_file():
            self.skipTest("assets/bin/7z.exe 不存在，跳过真实 7-Zip 端到端测试")

        src_zip = self.scratch / "真实压缩包.zip"
        with zipfile.ZipFile(src_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("sub/hello_中文.txt", "ShellBreaker 7-Zip OK!")

        dest = self.scratch / "real_out"
        pcts: list[int] = []
        engine = SevenZipEngine(bundled_7z)
        engine.extract(src_zip, dest, on_progress=pcts.append)

        extracted = dest / "sub" / "hello_中文.txt"
        self.assertTrue(extracted.is_file())
        self.assertEqual(extracted.read_text(encoding="utf-8"), "ShellBreaker 7-Zip OK!")
        self.assertIn(100, pcts)


# ==================== 3. 图种魔数偏移嗅探与端到端提取 ====================


class TestPhase3PolyglotMagicAndPipeline(_IsolatedConfigMixin, unittest.TestCase):
    """验证图种偏移检测、跨 1MB 边界扫描、防误报/白名单保护以及管线端到端提取。"""

    def setUp(self):
        self._setup_isolated_env("sb_p3_poly_")
        self.fake_exe = self.scratch / "fake_engine.exe"
        self.fake_exe.write_bytes(b"MZ")
        self.out_dir = self.scratch / "delivered"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.set("output_mode", "custom")
        self.cfg.set("custom_output_path", str(self.out_dir))
        self.cfg.set("enable_ad_filter", False)
        self.cfg.set("enable_nested_limit_confirm", False)
        self.cfg.set("min_file_size_mb", 10)

    def tearDown(self):
        self._teardown_isolated_env()

    def _make_zip_bytes(self, entries: dict[str, bytes]) -> bytes:
        zpath = self.scratch / f"_tmp_{uuid.uuid4().hex}.zip"
        with zipfile.ZipFile(zpath, "w", compression=zipfile.ZIP_STORED) as zf:
            for name, data in entries.items():
                zf.writestr(name, data)
        data = zpath.read_bytes()
        zpath.unlink(missing_ok=True)
        return data

    def test_detect_embedded_archive_polyglots_and_boundary_and_protections(self):
        # 1) JPEG + ZIP
        jpeg_prefix = b"\xff\xd8\xff\xe0" + b"\x11" * 124
        zip_payload = self._make_zip_bytes({"secret.txt": b"polyglot zip ok"})
        jpg_zip = self.scratch / "img_zip.jpg"
        jpg_zip.write_bytes(jpeg_prefix + zip_payload)
        self.assertEqual(detect_embedded_archive(jpg_zip), (".zip", len(jpeg_prefix)))
        self.assertEqual(detect_archive_type(jpg_zip), ".zip")

        # 2) PNG + RAR5（合法 RAR5 Main 头 CRC32）
        png_prefix = b"\x89PNG\r\n\x1a\n" + b"\x22" * 200
        rar5_body = b"\x01\x00\x00"  # head_type=1 (Main), flags=0, archive_flags=0
        rar5_size_vint = bytes([len(rar5_body)])
        rar5_crc = zlib.crc32(rar5_size_vint + rar5_body) & 0xFFFFFFFF
        rar5_magic = b"Rar!\x1a\x07\x01\x00" + struct.pack("<I", rar5_crc) + rar5_size_vint + rar5_body + b"\x00" * 16
        png_rar = self.scratch / "img_rar.png"
        png_rar.write_bytes(png_prefix + rar5_magic)
        self.assertEqual(detect_embedded_archive(png_rar), (".rar", len(png_prefix)))
        self.assertEqual(detect_archive_type(png_rar), ".rar")

        # 3) 跨 1MB (1048576) 块边界的合法 7z 32 字节 StartHeader
        boundary_offset = 1024 * 1024 - 2
        cross_7z = self.scratch / "cross_boundary.jpg"
        next_hdr_info = struct.pack("<QQI", 0, 16, 0)
        start_crc = zlib.crc32(next_hdr_info) & 0xFFFFFFFF
        valid_7z_hdr = b"\x37\x7a\xbc\xaf\x27\x1c\x00\x04" + struct.pack("<I", start_crc) + next_hdr_info
        with open(cross_7z, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0" + b"\x00" * (boundary_offset - 4))
            f.write(valid_7z_hdr + b"\x00" * 32)
        self.assertEqual(detect_embedded_archive(cross_7z), (".7z", boundary_offset))

        # 4) 纯真 MP4 含伪 PK\x03\x04 无 EOCD -> 绝不误报
        mp4_fake = self.scratch / "real_video.mp4"
        mp4_fake.write_bytes(
            b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00"
            + b"\xaa" * 512
            + b"PK\x03\x04"
            + b"\xbb" * 512
        )
        self.assertEqual(detect_embedded_archive(mp4_fake), (None, -1))
        self.assertIsNone(detect_archive_type(mp4_fake))

        # 5) .apk / .exe 终点白名单保护
        apk = self.scratch / "app.apk"
        apk.write_bytes(zip_payload)
        exe = self.scratch / "installer.exe"
        exe.write_bytes(b"MZ" + b"\x00" * 64 + zip_payload)
        self.assertEqual(detect_embedded_archive(apk), (None, -1))
        self.assertEqual(detect_embedded_archive(exe), (None, -1))
        self.assertIsNone(detect_archive_type(apk))
        self.assertIsNone(detect_archive_type(exe))

    def test_pipeline_polyglot_outer_slice_and_nested_disguised_extraction(self):
        # 场景 A：外层图种通过 fallback_extract 链 ④ 偏移剥离提取成功
        jpeg_prefix = b"\xff\xd8\xff\xe0" + b"\x33" * 96
        inner_zip = self._make_zip_bytes({"outer_payload.txt": b"sliced outer ok"})
        outer_poly = self.scratch / "outer_cover.jpg"
        outer_poly.write_bytes(jpeg_prefix + inner_zip)

        engine_a = _FakeZipOnlyOffsetZeroEngine(self.fake_exe)
        pipe_a = ExtractionPipeline(
            engine=engine_a,
            engine_name="MockOffset0",
            sandbox=SandboxManager(),
            password_manager=self.pwd_mgr,
            config=self.cfg,
        )
        ok_a = pipe_a.process("t_poly_outer", str(outer_poly))
        self.assertTrue(ok_a)
        delivered_a = self.out_dir / "outer_cover" / "outer_payload.txt"
        self.assertTrue(delivered_a.is_file())
        self.assertEqual(delivered_a.read_text(encoding="utf-8"), "sliced outer ok")

        # 场景 B：嵌套层内含小体积图种（<10MB 仍因确证图种放行）经 find_and_convert_disguised_archive 剥离提取成功
        nested_zip_bytes = self._make_zip_bytes({"deep_secret.txt": b"nested polyglot ok"})
        nested_poly_bytes = jpeg_prefix + nested_zip_bytes
        outer_zip = self.scratch / "nested_poly_pack.zip"
        with zipfile.ZipFile(outer_zip, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("art_disguise.jpg", nested_poly_bytes)

        engine_b = _FakeZipOnlyOffsetZeroEngine(self.fake_exe)
        pipe_b = ExtractionPipeline(
            engine=engine_b,
            engine_name="MockOffset0",
            sandbox=SandboxManager(),
            password_manager=self.pwd_mgr,
            config=self.cfg,
        )
        ok_b = pipe_b.process("t_poly_nested", str(outer_zip))
        self.assertTrue(ok_b)
        delivered_b = self.out_dir / "nested_poly_pack" / "deep_secret.txt"
        self.assertTrue(delivered_b.is_file())
        self.assertEqual(delivered_b.read_text(encoding="utf-8"), "nested polyglot ok")


# ==================== 4. 分卷连续性预检与队列拦截 ====================


class TestPhase3VolumeCompletenessAndQueueGuard(_IsolatedConfigMixin, unittest.TestCase):
    """验证分卷连续性预检、TaskQueue 入队拦截、StatusPill 与 MainWindow 终态备注保护。"""

    def setUp(self):
        self._setup_isolated_env("sb_p3_vol_")
        self.cfg.set("min_file_size_mb", 0)

    def tearDown(self):
        self._teardown_isolated_env()

    def test_check_volume_completeness_all_patterns_and_complete_cases(self):
        # 1) .part1.rar + .part3.rar -> 缺 part2
        d1 = self.scratch / "case_part1"
        d1.mkdir()
        p1 = d1 / "movie.part1.rar"
        p1.write_bytes(b"1")
        (d1 / "movie.part3.rar").write_bytes(b"3")
        self.assertEqual(check_volume_completeness(p1), (False, ["part2"]))

        # 2) .part01.rar + .part03.rar -> 缺 part02
        d2 = self.scratch / "case_part01"
        d2.mkdir()
        p01 = d2 / "game.part01.rar"
        p01.write_bytes(b"1")
        (d2 / "game.part03.rar").write_bytes(b"3")
        self.assertEqual(check_volume_completeness(p01), (False, ["part02"]))

        # 3) .7z.001 + .7z.003 -> 缺 002
        d3 = self.scratch / "case_7z"
        d3.mkdir()
        s001 = d3 / "pack.7z.001"
        s001.write_bytes(b"1")
        (d3 / "pack.7z.003").write_bytes(b"3")
        self.assertEqual(check_volume_completeness(s001), (False, ["002"]))

        # 4) .rar + .r00 + .r02 -> 缺 r01
        d4 = self.scratch / "case_r00"
        d4.mkdir()
        crar = d4 / "old.rar"
        crar.write_bytes(b"0")
        (d4 / "old.r00").write_bytes(b"1")
        (d4 / "old.r02").write_bytes(b"2")
        self.assertEqual(check_volume_completeness(crar), (False, ["r01"]))

        # 5) .zip + .z01 + .z03 -> 缺 z02
        d5 = self.scratch / "case_z01"
        d5.mkdir()
        czip = d5 / "legacy.zip"
        czip.write_bytes(b"0")
        (d5 / "legacy.z01").write_bytes(b"1")
        (d5 / "legacy.z03").write_bytes(b"3")
        self.assertEqual(check_volume_completeness(czip), (False, ["z02"]))

        # 6) 完整分卷与单卷 -> (True, [])
        (d2 / "game.part02.rar").write_bytes(b"2")
        self.assertEqual(check_volume_completeness(p01), (True, []))

        (d3 / "pack.7z.002").write_bytes(b"2")
        self.assertEqual(check_volume_completeness(s001), (True, []))

        (d4 / "old.r01").write_bytes(b"1")
        self.assertEqual(check_volume_completeness(crar), (True, []))

        (d5 / "legacy.z02").write_bytes(b"2")
        self.assertEqual(check_volume_completeness(czip), (True, []))

        single = self.scratch / "solo.zip"
        single.write_bytes(b"PK\x03\x04")
        self.assertEqual(check_volume_completeness(single), (True, []))

    def test_task_queue_submit_blocks_incomplete_volume_without_threadpool(self):
        vdir = self.scratch / "broken_vol"
        vdir.mkdir()
        p1 = vdir / "archive.part1.rar"
        p1.write_bytes(b"Rar!" + b"0" * 16)
        (vdir / "archive.part3.rar").write_bytes(b"0" * 16)

        tq = TaskQueue()
        tq.start()
        try:
            registered: list[tuple[str, str]] = []
            statuses: list[tuple[str, str, str, str, str]] = []
            tq.task_registered.connect(lambda tid, sp: registered.append((tid, sp)))
            tq.task_status_changed.connect(lambda *a: statuses.append(a))

            with patch.object(tq._executor, "submit") as mock_pool_submit:
                tid = tq.submit(str(p1))
                mock_pool_submit.assert_not_called()

            self.assertIsNotNone(tid)
            self.assertEqual(len(registered), 1)
            self.assertEqual(registered[0], (tid, str(p1)))
            self.assertEqual(len(statuses), 1)
            self.assertEqual(statuses[0][0], tid)
            self.assertEqual(statuses[0][1], "archive.part1.rar")
            self.assertEqual(statuses[0][2], "失败(缺失分卷: part2)")
            self.assertIn("part2", statuses[0][4])

            # submit_existing 同样拦截不入线程池
            with patch.object(tq._executor, "submit") as mock_pool_submit2:
                tq.submit_existing("t_exist_mv", str(p1))
                mock_pool_submit2.assert_not_called()
            self.assertEqual(statuses[-1][2], "失败(缺失分卷: part2)")
        finally:
            tq.shutdown(wait=True)

    def test_status_pill_and_main_window_missing_volume_terminal_and_remark_protection(self):
        status_text = "失败(缺失分卷: part2)"
        self.assertEqual(
            status_palette(LIGHT_TOKENS, status_text),
            (LIGHT_TOKENS.danger, LIGHT_TOKENS.danger_bg),
        )
        self.assertEqual(
            status_palette(DARK_TOKENS, status_text),
            (DARK_TOKENS.danger, DARK_TOKENS.danger_bg),
        )
        self.assertTrue(_is_terminal_status(status_text))

        # StatusPillDelegate 离屏绘制不报错
        delegate = StatusPillDelegate()
        img = QImage(220, 44, QImage.Format.Format_ARGB32)
        painter = QPainter(img)
        opt = QStyleOptionViewItem()
        opt.rect = QRect(0, 0, 220, 44)
        mock_idx = MagicMock()
        mock_idx.data.return_value = status_text
        try:
            delegate.paint(painter, opt, mock_idx)
        finally:
            painter.end()

        win = MainWindow()
        try:
            remark_orig = "检测到分卷不完整，缺少: part2"
            win._on_task_status_changed("t_mv", "archive.part1.rar", status_text, "7-Zip", remark_orig)
            row = win._task_rows["t_mv"]
            self.assertEqual(win._get_cell_text(row, TasksPage.COL_STATUS), status_text)
            self.assertEqual(win._get_cell_text(row, TasksPage.COL_REMARK), remark_orig)
            self.assertEqual(win.tasks_page.status_bar._labels["failed"].text(), "失败 1")
            self.assertEqual(win._progress_widgets["t_mv"].terminal_state, status_text)
            self.assertEqual(win._progress_widgets["t_mv"]._right_label_text(), "失败")

            # 迟到的进度与层级进度绝不能覆盖 COL_REMARK
            win._on_task_progress("t_mv", "迟到的普通进度")
            win._on_task_layer_progress("t_mv", 50, 1, "迟到的层级进度")
            self.assertEqual(win._get_cell_text(row, TasksPage.COL_REMARK), remark_orig)
        finally:
            win.close()


# ==================== 5. 删除三模式与推广垃圾文件黑名单过滤 ====================


class TestPhase3DeleteModesAndAdFilter(_IsolatedConfigMixin, unittest.TestCase):
    """验证 handle_delete_source 三模式与 filter_ad_files 黑名单过滤及安全底线。"""

    def setUp(self):
        self._setup_isolated_env("sb_p3_del_")
        self.fake_exe = self.scratch / "fake_engine.exe"
        self.fake_exe.write_bytes(b"MZ")

    def tearDown(self):
        self._teardown_isolated_env()

    def test_handle_delete_source_three_modes_with_split_volumes(self):
        pipe = ExtractionPipeline(
            engine=_FakeZipOnlyOffsetZeroEngine(self.fake_exe),
            engine_name="Mock",
            sandbox=SandboxManager(),
            password_manager=self.pwd_mgr,
            config=self.cfg,
        )

        # 1) keep：全部保留，不调用回收站
        d_keep = self.scratch / "keep_case"
        d_keep.mkdir()
        k1 = d_keep / "pack.part01.rar"
        k2 = d_keep / "pack.part02.rar"
        k1.write_bytes(b"1")
        k2.write_bytes(b"2")
        self.cfg.set("delete_source_mode", "keep")
        with patch("core.pipeline.send_to_recycle_bin") as mock_rec:
            pipe.handle_delete_source("t_keep", k1)
            mock_rec.assert_not_called()
        self.assertTrue(k1.exists() and k2.exists())

        # 2) recycle_bin：对同族全部分卷调用 send_to_recycle_bin
        d_rec = self.scratch / "rec_case"
        d_rec.mkdir()
        r1 = d_rec / "pack.part01.rar"
        r2 = d_rec / "pack.part02.rar"
        r1.write_bytes(b"1")
        r2.write_bytes(b"2")
        self.cfg.set("delete_source_mode", "recycle_bin")
        with patch("core.pipeline.send_to_recycle_bin", return_value=True) as mock_rec:
            pipe.handle_delete_source("t_rec", r1)
            called_paths = {Path(c.args[0]).resolve() for c in mock_rec.call_args_list}
        self.assertEqual(called_paths, {r1.resolve(), r2.resolve()})

        # 3) permanent：物理删除同族全部分卷，不调用回收站
        d_perm = self.scratch / "perm_case"
        d_perm.mkdir()
        m1 = d_perm / "pack.part01.rar"
        m2 = d_perm / "pack.part02.rar"
        m1.write_bytes(b"1")
        m2.write_bytes(b"2")
        self.cfg.set("delete_source_mode", "permanent")
        with patch("core.pipeline.send_to_recycle_bin") as mock_rec:
            pipe.handle_delete_source("t_perm", m1)
            mock_rec.assert_not_called()
        self.assertFalse(m1.exists())
        self.assertFalse(m2.exists())

    def test_filter_ad_files_case_insensitive_empty_subdir_and_safety_floor(self):
        out = self.scratch / "ad_out"
        (out / "ad_only_dir" / "nested").mkdir(parents=True)
        (out / "docs").mkdir(parents=True)

        keep_main = out / "Game.exe"
        keep_main.write_bytes(b"MZ_GAME")
        keep_readme = out / "docs" / "readme.txt"
        keep_readme.write_text("normal doc", encoding="utf-8")

        ad1 = out / "ad_only_dir" / "nested" / "访问官网.URL"
        ad1.write_text("[InternetShortcut]", encoding="utf-8")
        ad2 = out / "ad_only_dir" / "扫码关注领福利.PNG"
        ad2.write_bytes(b"img")
        ad3 = out / "docs" / "资源免责声明必看.TXT"
        ad3.write_text("disclaimer", encoding="utf-8")

        removed = ExtractionPipeline.filter_ad_files(out, DEFAULT_CONFIG["ad_filter_patterns"])
        self.assertEqual(removed, 3)
        self.assertTrue(keep_main.is_file())
        self.assertTrue(keep_readme.is_file())
        self.assertFalse(ad1.exists())
        self.assertFalse(ad2.exists())
        self.assertFalse(ad3.exists())
        # 仅装广告文件的空子目录已被递归清理，但非空 docs 和 out 根目录保留
        self.assertFalse((out / "ad_only_dir").exists())
        self.assertTrue((out / "docs").is_dir())
        self.assertTrue(out.is_dir())

        # 安全底线：目录内全部文件均命中黑名单 -> 保留全部文件并返回 0
        all_ads_dir = self.scratch / "all_ads"
        all_ads_dir.mkdir()
        a1 = all_ads_dir / "site.url"
        a2 = all_ads_dir / "扫码关注.jpg"
        a1.write_text("url", encoding="utf-8")
        a2.write_bytes(b"jpg")

        removed_floor = ExtractionPipeline.filter_ad_files(
            all_ads_dir, DEFAULT_CONFIG["ad_filter_patterns"]
        )
        self.assertEqual(removed_floor, 0)
        self.assertTrue(a1.is_file())
        self.assertTrue(a2.is_file())

    def test_pipeline_process_delivers_ad_free_output(self):
        delivered_root = self.scratch / "clean_deliver"
        delivered_root.mkdir()
        self.cfg.set("output_mode", "custom")
        self.cfg.set("custom_output_path", str(delivered_root))
        self.cfg.set("enable_ad_filter", True)
        self.cfg.set("delete_source_mode", "keep")

        src_zip = self.scratch / "bundle.zip"
        with zipfile.ZipFile(src_zip, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("main_movie.mkv", b"VIDEO_DATA")
            zf.writestr("最新地址发布.url", b"[InternetShortcut]")
            zf.writestr("promo/扫码关注公众号.jpg", b"AD_IMG")

        pipe = ExtractionPipeline(
            engine=_FakeZipOnlyOffsetZeroEngine(self.fake_exe),
            engine_name="Mock",
            sandbox=SandboxManager(),
            password_manager=self.pwd_mgr,
            config=self.cfg,
        )
        self.assertTrue(pipe.process("t_ad_pipe", str(src_zip)))

        final_dir = delivered_root / "bundle"
        self.assertTrue((final_dir / "main_movie.mkv").is_file())
        self.assertFalse((final_dir / "最新地址发布.url").exists())
        self.assertFalse((final_dir / "promo").exists())


# ==================== 6. 系统集成（单实例 IPC + Watch Folder） ====================


class TestPhase3SystemIntegration(_IsolatedConfigMixin, unittest.TestCase):
    """验证单实例 IPC 中文路径转发与 WatchFolderService 稳定性检测。"""

    def setUp(self):
        self._setup_isolated_env("sb_p3_sys_")

    def tearDown(self):
        self._teardown_isolated_env()

    def test_single_instance_bridge_chinese_paths_and_empty_list(self):
        srv_name = f"ShellBreaker_TestIPC_{uuid.uuid4().hex}"
        server = SingleInstanceBridge(server_name=srv_name)
        received: list[list[str]] = []
        server.paths_received.connect(lambda p: received.append(list(p)))

        self.assertTrue(server.start_server())
        try:
            cn_paths = [r"E:\中文目录\压缩包一.zip", r"D:\素材合集\测试 文件夹"]
            sub_code = (
                "import json, os, sys; "
                f"sys.path.insert(0, {str(PROJECT_ROOT)!r}); "
                "os.environ['QT_QPA_PLATFORM'] = 'offscreen'; "
                "from PySide6.QtCore import QCoreApplication; "
                "app = QCoreApplication(sys.argv); "
                "from core.system_integration import SingleInstanceBridge; "
                f"cli = SingleInstanceBridge(server_name={srv_name!r}); "
                f"r1 = cli.try_forward_to_existing({cn_paths!r}, timeout_ms=2000); "
                "r2 = cli.try_forward_to_existing([], timeout_ms=2000); "
                "print(json.dumps([r1, r2]))"
            )
            env = dict(os.environ)
            env["QT_QPA_PLATFORM"] = "offscreen"
            proc = subprocess.Popen(
                [sys.executable, "-c", sub_code],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=env,
            )
            try:
                self.assertTrue(
                    _wait_until(
                        lambda: proc.poll() is not None and len(received) >= 2,
                        timeout_s=5.0,
                        step_ms=15,
                    ),
                    f"IPC 子进程转发超时 (poll={proc.poll()}, received={received})",
                )
                out, err = proc.communicate(timeout=2.0)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=2.0)

            self.assertEqual(proc.returncode, 0, f"IPC 子进程异常: {err}")
            self.assertEqual(json.loads(out.strip()), [True, True])
            self.assertEqual(received, [cn_paths, []])
        finally:
            server.stop_server()

    def test_watch_folder_service_ignores_existing_temp_secondary_and_emits_once_after_rename(self):
        watch_dir = self.scratch / "watch_root"
        watch_dir.mkdir()
        existing_zip = watch_dir / "preexisting.zip"
        existing_zip.write_bytes(b"PK\x03\x04old")

        svc = WatchFolderService(stable_seconds=0.2, poll_interval_ms=60)
        ready_files: list[str] = []
        svc.file_ready.connect(ready_files.append)

        try:
            self.assertTrue(svc.start(watch_dir))
            self.assertTrue(svc.is_running())
            self.assertEqual(svc.watch_dir(), watch_dir.resolve())

            # 新增临时下载文件与非首卷分卷：均应被忽略
            crdl = watch_dir / "incoming.crdownload"
            crdl.write_bytes(b"PK\x03\x04" + b"x" * 128)
            sec_part = watch_dir / "movie.part02.rar"
            sec_part.write_bytes(b"Rar!" + b"y" * 128)

            _pump_events(320)
            self.assertEqual(ready_files, [])

            # 将 .crdownload 重命名为 .zip，体积稳定后应精准发射一次 file_ready
            final_zip = watch_dir / "incoming.zip"
            os.replace(crdl, final_zip)

            self.assertTrue(
                _wait_until(lambda: len(ready_files) == 1, timeout_s=1.2, step_ms=30),
                f"预期触发一次 file_ready，实际收到: {ready_files}",
            )
            self.assertEqual(Path(ready_files[0]).resolve(), final_zip.resolve())

            # 继续等待数个轮询周期，确认不会重复发射
            _pump_events(250)
            self.assertEqual(len(ready_files), 1)
        finally:
            svc.stop()
            self.assertFalse(svc.is_running())
            self.assertIsNone(svc.watch_dir())


# ==================== 7. 动态主题切换、设置页 P2 控件、MainWindow 与 CLI ====================


class TestPhase3ThemeSettingsMainWindowAndCLI(_IsolatedConfigMixin, unittest.TestCase):
    """验证主题优先级与广播、SettingsPage 全量 P2 控件、MainWindow 联动及 main.extract_cli_paths。"""

    def setUp(self):
        self._setup_isolated_env("sb_p3_ui_")
        self._orig_env_theme = os.environ.get("SHELLBREAKER_THEME")
        self._orig_theme_tokens = tm_mod._CURRENT_THEME
        self._orig_runtime_override = tm_mod._RUNTIME_OVERRIDE
        self._app = QApplication.instance()
        self._orig_app_prop = self._app.property(tm_mod._THEME_PROPERTY) if self._app else None

    def tearDown(self):
        if self._orig_env_theme is None:
            os.environ.pop("SHELLBREAKER_THEME", None)
        else:
            os.environ["SHELLBREAKER_THEME"] = self._orig_env_theme
        tm_mod._CURRENT_THEME = self._orig_theme_tokens
        tm_mod._RUNTIME_OVERRIDE = self._orig_runtime_override
        if self._app is not None:
            set_theme("light", self._app)
            tm_mod._RUNTIME_OVERRIDE = self._orig_runtime_override
            if self._orig_app_prop is not None:
                self._app.setProperty(tm_mod._THEME_PROPERTY, self._orig_app_prop)
        self._teardown_isolated_env()

    def test_theme_manager_priority_and_runtime_set_theme_broadcast(self):
        app = QApplication.instance()

        # 1) 无环境变量时读取 ConfigManager["theme"]，兜底 light
        os.environ.pop("SHELLBREAKER_THEME", None)
        self.cfg.set("theme", "light")
        self.assertEqual(ensure_theme(app).name, LIGHT_TOKENS.name)

        self.cfg.set("theme", "dark")
        self.assertEqual(ensure_theme(app).name, DARK_TOKENS.name)

        # 2) SHELLBREAKER_THEME 环境变量优先级高于 ConfigManager["theme"]
        os.environ["SHELLBREAKER_THEME"] = "light"
        self.cfg.set("theme", "dark")
        self.assertEqual(ensure_theme(app).name, LIGHT_TOKENS.name)

        os.environ["SHELLBREAKER_THEME"] = "dark"
        self.cfg.set("theme", "light")
        self.assertEqual(ensure_theme(app).name, DARK_TOKENS.name)

        # 3) 运行时 set_theme 动态切换与信号广播
        os.environ.pop("SHELLBREAKER_THEME", None)
        emitted: list[str] = []
        notifier = get_theme_notifier()
        notifier.theme_changed.connect(emitted.append)
        try:
            self.assertEqual(set_theme("dark", app), "dark")
            self.assertEqual(get_current_theme().name, DARK_TOKENS.name)
            self.assertEqual(app.property(tm_mod._THEME_PROPERTY), DARK_TOKENS.name)

            self.assertEqual(set_theme("light", app), "light")
            self.assertEqual(get_current_theme().name, LIGHT_TOKENS.name)
            self.assertEqual(app.property(tm_mod._THEME_PROPERTY), LIGHT_TOKENS.name)
        finally:
            notifier.theme_changed.disconnect(emitted.append)

        self.assertEqual(emitted, ["dark", "light"])

    def test_settings_page_all_p2_controls_and_persistence(self):
        fake_7z = self.scratch / "custom_7z.exe"
        fake_7z.write_bytes(b"MZ")
        watch_dir = self.scratch / "watch_cfg_dir"
        watch_dir.mkdir()

        self.assertEqual(
            parse_ad_filter_patterns("*.url， *广告*, *.url,  *推广* \n "),
            ["*.url", "*广告*", "*推广*"],
        )

        page = SettingsPage()
        try:
            self.assertFalse(hasattr(page, "cb_context_menu"))

            # 1) 7zip 引擎与路径
            idx_7z = page.combo_engine.findData("7zip")
            self.assertGreaterEqual(idx_7z, 0)
            page.combo_engine.setCurrentIndex(idx_7z)
            self.assertEqual(self.cfg.get("default_engine"), "7zip")

            page.picker_sevenzip.set_path(str(fake_7z))
            page.picker_sevenzip.path_changed.emit(str(fake_7z))
            self.assertEqual(self.cfg.get("sevenzip_path"), str(fake_7z))

            # 2) 三档删除下拉框 + 兼容隐藏开关双向同步
            idx_perm = page.combo_delete_source_mode.findData("permanent")
            page.combo_delete_source_mode.setCurrentIndex(idx_perm)
            self.assertEqual(self.cfg.get("delete_source_mode"), "permanent")
            self.assertIs(self.cfg.get("delete_source_after_extract"), True)
            self.assertTrue(page.cb_delete_source.isChecked())

            page.cb_delete_source.setChecked(False)
            self.assertEqual(self.cfg.get("delete_source_mode"), "keep")
            self.assertEqual(page.combo_delete_source_mode.currentData(), "keep")

            # 3) 推广过滤开关与规则输入框
            page.cb_ad_filter.setChecked(False)
            self.assertIs(self.cfg.get("enable_ad_filter"), False)
            page.cb_ad_filter.setChecked(True)
            self.assertIs(self.cfg.get("enable_ad_filter"), True)

            page.edit_ad_patterns.setText("*.url， *宣传*, *.url")
            page.edit_ad_patterns.editingFinished.emit()
            self.assertEqual(self.cfg.get("ad_filter_patterns"), ["*.url", "*宣传*"])

            # 4) 监听目录开关与路径
            page.picker_watch_folder.set_path(str(watch_dir))
            page.picker_watch_folder.path_changed.emit(str(watch_dir))
            page.cb_watch_folder.setChecked(True)
            self.assertEqual(self.cfg.get("watch_folder_path"), str(watch_dir))
            self.assertIs(self.cfg.get("enable_watch_folder"), True)

            # 5) 外观主题切换与持久化
            idx_dark = page.combo_theme.findData("dark")
            page.combo_theme.setCurrentIndex(idx_dark)
            self.assertEqual(self.cfg.get("theme"), "dark")
            self.assertEqual(get_current_theme().name, DARK_TOKENS.name)

            idx_light = page.combo_theme.findData("light")
            page.combo_theme.setCurrentIndex(idx_light)
            self.assertEqual(self.cfg.get("theme"), "light")
            self.assertEqual(get_current_theme().name, LIGHT_TOKENS.name)
        finally:
            page.close()

        # 确认磁盘 JSON 已真实持久化
        disk_cfg = json.loads(self.tmp_cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(disk_cfg["default_engine"], "7zip")
        self.assertEqual(disk_cfg["sevenzip_path"], str(fake_7z))
        self.assertEqual(disk_cfg["ad_filter_patterns"], ["*.url", "*宣传*"])
        self.assertEqual(disk_cfg["watch_folder_path"], str(watch_dir))
        self.assertIs(disk_cfg["enable_watch_folder"], True)
        self.assertEqual(disk_cfg["theme"], "light")

    def test_main_window_theme_watch_sync_enqueue_cli_and_extract_cli_paths(self):
        # 1) main.extract_cli_paths 解析：跳过 argv[0]、选项标志、空值、剥引号、大小写无关去重
        p_a = self.scratch / "demo_a.zip"
        p_b = self.scratch / "子目录" / "demo_b.7z"
        parsed = main_mod.extract_cli_paths(
            [
                "ShellBreaker.exe",
                "--minimized",
                "-v",
                "",
                "   ",
                f'"{p_a}"',
                str(p_a),
                str(p_b),
            ]
        )
        self.assertEqual(
            [Path(x).resolve() for x in parsed],
            [p_a.resolve(), p_b.resolve()],
        )

        # 2) MainWindow 换肤刷新、WatchFolder 启停联动、enqueue_cli_paths 入队
        watch_dir = self.scratch / "mw_watch"
        watch_dir.mkdir()
        p_a.write_bytes(b"PK\x03\x04" + b"0" * 64)
        missing_path = self.scratch / "does_not_exist.zip"
        self.cfg.set("min_file_size_mb", 0)

        win = MainWindow()
        try:
            # 动态换肤刷新：SmoothLabel 颜色随主题同步
            set_theme("dark")
            self.assertEqual(
                win.tasks_page.title_label.text_color().name().lower(),
                DARK_TOKENS.text.lower(),
            )
            set_theme("light")
            self.assertEqual(
                win.tasks_page.title_label.text_color().name().lower(),
                LIGHT_TOKENS.text.lower(),
            )

            # WatchFolderService 启停同步
            self.assertFalse(win.watch_service.is_running())
            self.cfg.set("watch_folder_path", str(watch_dir))
            self.cfg.set("enable_watch_folder", True)
            win._on_settings_page_changed("enable_watch_folder")
            self.assertTrue(win.watch_service.is_running())
            self.assertEqual(win.watch_service.watch_dir(), watch_dir.resolve())

            self.cfg.set("enable_watch_folder", False)
            win._on_settings_page_changed("enable_watch_folder")
            self.assertFalse(win.watch_service.is_running())

            # enqueue_cli_paths：自动切回任务页、去重并忽略不存在路径
            win.nav.set_current_index(2)
            win.stack.setCurrentIndex(2)
            with patch.object(win._task_queue, "submit_existing") as mock_submit:
                accepted = win.enqueue_cli_paths([str(p_a), str(p_a), str(missing_path)])
                self.assertEqual(accepted, 1)
                mock_submit.assert_called_once()
            self.assertEqual(win.stack.currentIndex(), 0)
            self.assertEqual(win._table.rowCount(), 1)
            self.assertEqual(win._get_cell_text(0, TasksPage.COL_FILENAME), "demo_a.zip")
        finally:
            win.close()

    def test_native_dialog_and_content_dialog_offscreen_rendering_light_and_dark(self):
        """验证浅色/深色主题下 QMessageBox / QInputDialog 渲染背景非纯黑，且 ContentDialog 圆角透明属性完好。"""
        app = QApplication.instance()
        for mode, expected_tokens in (("light", LIGHT_TOKENS), ("dark", DARK_TOKENS)):
            set_theme(mode, app)

            box = QMessageBox(
                QMessageBox.Icon.Information,
                "提示",
                "测试原生弹窗背景与文字对比度",
            )
            ensure_theme(box)
            box.adjustSize()
            try:
                box_img = box.grab().toImage()
                corner = box_img.pixelColor(2, 2).name().lower()
                top_mid = box_img.pixelColor(box_img.width() // 2, 2).name().lower()
                self.assertNotEqual(corner, "#000000", f"{mode} 下 QMessageBox 角落不能为纯黑")
                self.assertNotEqual(top_mid, "#000000", f"{mode} 下 QMessageBox 顶部不能为纯黑")
                self.assertEqual(corner, expected_tokens.card.lower())
                self.assertEqual(top_mid, expected_tokens.card.lower())
            finally:
                box.close()

            inp = QInputDialog()
            inp.setWindowTitle("输入")
            inp.setLabelText("请输入内容：")
            ensure_theme(inp)
            inp.adjustSize()
            try:
                inp_img = inp.grab().toImage()
                inp_corner = inp_img.pixelColor(2, 2).name().lower()
                self.assertNotEqual(inp_corner, "#000000", f"{mode} 下 QInputDialog 角落不能为纯黑")
                self.assertEqual(inp_corner, expected_tokens.card.lower())
            finally:
                inp.close()

            dlg = ContentDialog("无边框弹窗", "内容说明")
            dlg.adjustSize()
            try:
                self.assertTrue(bool(dlg.property("framelessDialog")))
                self.assertTrue(dlg.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
                dlg_img = dlg.grab().toImage()
                outer_px = dlg_img.pixelColor(2, 2)
                card_px = dlg_img.pixelColor(dlg_img.width() // 2, dlg_img.height() // 2)
                self.assertEqual(outer_px.alpha(), 0)
                self.assertEqual(card_px.name().lower(), expected_tokens.card.lower())
            finally:
                dlg.close()

            pwd_dlg = PasswordDialog()
            try:
                pwd_img = pwd_dlg.grab().toImage()
                pwd_corner = pwd_img.pixelColor(2, 2).name().lower()
                self.assertNotEqual(pwd_corner, "#000000")
                self.assertEqual(pwd_corner, expected_tokens.window.lower())
            finally:
                pwd_dlg.close()

            set_dlg = SettingsDialog()
            try:
                set_img = set_dlg.grab().toImage()
                set_corner = set_img.pixelColor(2, 2).name().lower()
                self.assertNotEqual(set_corner, "#000000")
                self.assertEqual(set_corner, expected_tokens.window.lower())
            finally:
                set_dlg.close()


if __name__ == "__main__":
    unittest.main()
