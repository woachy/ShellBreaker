"""
v1.2.1 核心架构修复与回归测试套件：
1. 7z / RAR4 / RAR5 假魔数防误报 + 合法头部强校验 + 假命中后继续扫描不提前放弃 + 偏移 0 宽容
2. 大载体 MP4+ZIP 图种 O(1) EOCD 精准定位（无 64MB~size-8MB 盲区）+ .apk/.exe/.msi 白名单保护
3. 伪装包链 ② 硬链接零拷贝不短路（不误删原文件 + 双计数器 total == 1 + 密码异常阻断降级）
4. Bandizip CLI 统一纠偏 resolve_bandizip_cli（BandizipEngine / discover_engine / SettingsPage）
5. WinRAREngine.repair_extract 守卫与不确定态进度(-1) + 退出码 10 加密 ZIP 精确分类 + 图种 test_password 容错
6. 真实 assets/bin/7z.exe 对大偏移加密 MP4+ZIP 图种端到端密码解压验证

全部用例支持 QT_QPA_PLATFORM=offscreen 离屏运行，严禁污染真实 data/config.json / data/passwords.json。
"""
from __future__ import annotations

import copy
import os
import shutil
import struct
import subprocess
import sys
import tempfile
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

from PySide6.QtWidgets import QApplication

_APP = QApplication.instance() or QApplication(sys.argv)

import core.engine as engine_mod
import core.pipeline as pipeline_mod
import core.sandbox as sandbox_mod
import data.config_manager as cfg_mod
from core.engine import (
    BandizipEngine,
    BaseEngine,
    EngineType,
    SevenZipEngine,
    WinRAREngine,
    _is_encrypted_zip,
    discover_engine,
    resolve_bandizip_cli,
)
from core.exceptions import (
    EngineNotFoundError,
    ExtractionFailedError,
    PasswordRequiredError,
    WrongPasswordError,
)
from core.pipeline import ExtractionPipeline
from core.sandbox import (
    SandboxManager,
    detect_archive_type,
    detect_embedded_archive,
    robust_rmtree,
)
from data.config_manager import ConfigManager, DEFAULT_CONFIG
from data.password_manager import PasswordManager
from ui.pages.settings_page import SettingsPage


# ==================== 测试构造辅助函数 ====================


def _make_valid_7z_bytes(payload_size: int = 32) -> bytes:
    """构造满足 7z 32 字节 StartHeader CRC32 与边界校验的最小合法 7z 数据块。"""
    next_offset = 0
    next_size = payload_size
    next_crc = 0
    next_info = struct.pack("<QQI", next_offset, next_size, next_crc)
    start_crc = zlib.crc32(next_info) & 0xFFFFFFFF
    header = b"\x37\x7a\xbc\xaf\x27\x1c\x00\x04" + struct.pack("<I", start_crc) + next_info
    return header + (b"\x5a" * payload_size)


def _make_valid_rar4_bytes() -> bytes:
    """构造满足 RAR4 7B 签名 + MAIN_HEAD (0x73, size=13, CRC16) 校验的最小合法 RAR4 数据块。"""
    marker = b"Rar!\x1a\x07\x00"
    head_type = 0x73
    head_flags = 0x0000
    head_size = 13
    body_after_crc = struct.pack("<BHH", head_type, head_flags, head_size) + (b"\x00" * 6)
    head_crc = zlib.crc32(body_after_crc) & 0xFFFF
    main_hdr = struct.pack("<H", head_crc) + body_after_crc
    return marker + main_hdr + b"\x00" * 16


def _make_valid_rar5_bytes(head_type: int = 1) -> bytes:
    """构造满足 RAR5 8B 签名 + CRC32 + vint head_size + 块类型 (1=Main/4=Encryption) 的最小合法 RAR5 数据块。"""
    marker = b"Rar!\x1a\x07\x01\x00"
    head_body = bytes([head_type, 0x00, 0x00])
    vint_size = bytes([len(head_body)])
    head_crc = zlib.crc32(vint_size + head_body) & 0xFFFFFFFF
    return marker + struct.pack("<I", head_crc) + vint_size + head_body + b"\x00" * 16


def _make_zip_bytes(entries: dict[str, bytes], scratch: Path) -> bytes:
    tmp = scratch / f"_tmp_{uuid.uuid4().hex}.zip"
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    raw = tmp.read_bytes()
    tmp.unlink(missing_ok=True)
    return raw


def _make_encrypted_flag_zip_bytes(scratch: Path) -> bytes:
    """构造合法 ZIP 结构并将首个成员中央目录及本地头的 flag_bits bit 0 (0x1) 置位（无需外部工具）。"""
    raw = bytearray(_make_zip_bytes({"secret.txt": b"encrypted payload bytes"}, scratch))
    # 设置 Local File Header (PK\x03\x04) offset +6 的 general purpose bit flag bit 0
    lfh = raw.find(b"PK\x03\x04")
    assert lfh != -1
    lfh_flags = struct.unpack_from("<H", raw, lfh + 6)[0]
    struct.pack_into("<H", raw, lfh + 6, lfh_flags | 0x0001)
    # 设置 Central Directory File Header (PK\x01\x02) offset +8 的 general purpose bit flag bit 0
    cdh = raw.find(b"PK\x01\x02")
    assert cdh != -1
    cdh_flags = struct.unpack_from("<H", raw, cdh + 8)[0]
    struct.pack_into("<H", raw, cdh + 8, cdh_flags | 0x0001)
    return bytes(raw)


class _IsolatedEnvMixin:
    def _setup_env(self, prefix: str = "sb_v121_") -> None:
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

    def _teardown_env(self) -> None:
        sandbox_mod.SANDBOX_ROOT = self._orig_sandbox_root
        with self.cfg._lock:
            cfg_mod.CONFIG_PATH = self._orig_cfg_path
            self.cfg._data = self._orig_cfg_data
            self.cfg._loaded = self._orig_cfg_loaded

        self.pwd_mgr._file = self._orig_pwd_file
        self.pwd_mgr._cache = self._orig_pwd_cache
        self.pwd_mgr._loaded = self._orig_pwd_loaded
        robust_rmtree(self.scratch)


# ==================== 场景 1 & 2：魔数防误报、强校验、EOCD 与白名单 ====================


class TestV121MagicAndPolyglotDetection(_IsolatedEnvMixin, unittest.TestCase):
    def setUp(self):
        self._setup_env("sb_v121_magic_")

    def tearDown(self):
        self._teardown_env()

    def test_scene_1_fake_7z_and_rar_magics_rejected_and_valid_headers_detected_after_fakes(self):
        mp4_hdr = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2mp41"

        # 1) 纯真 MP4 尾窗注入裸 7z / RAR4 / RAR5 / PK\x03\x04 随机假魔数 -> 全部返回 (None, -1)
        fake_mp4 = self.scratch / "pure_video_with_noise.mp4"
        fake_mp4.write_bytes(
            mp4_hdr
            + b"\xaa" * 2048
            + b"\x37\x7a\xbc\xaf\x27\x1c" + b"\xde\xad\xbe\xef" * 16
            + b"\xbb" * 512
            + b"Rar!\x1a\x07\x00" + b"\x12\x34\x56\x78" * 16
            + b"\xcc" * 512
            + b"Rar!\x1a\x07\x01\x00" + b"\xfe\xed\xfa\xce" * 16
            + b"\xdd" * 512
            + b"PK\x03\x04" + b"\x99" * 256
        )
        self.assertEqual(detect_embedded_archive(fake_mp4), (None, -1))
        self.assertIsNone(detect_archive_type(fake_mp4))

        # 2) 合法 32B CRC32 的真 7z（前面有假 7z 魔数干扰 -> 必须 pos+=1 继续扫描命中真 7z）
        valid_7z = _make_valid_7z_bytes(48)
        prefix_with_fake_7z = (
            mp4_hdr
            + b"\x11" * 100
            + b"\x37\x7a\xbc\xaf\x27\x1c" + b"\xff" * 40  # 假 7z
            + b"\x22" * 100
        )
        real_7z_mp4 = self.scratch / "fake_then_real_7z.mp4"
        real_7z_mp4.write_bytes(prefix_with_fake_7z + valid_7z)
        self.assertEqual(
            detect_embedded_archive(real_7z_mp4),
            (".7z", len(prefix_with_fake_7z)),
        )

        # 3) 假 RAR4 后紧跟合法 RAR4 -> 继续扫描精确命中合法 RAR4
        valid_rar4 = _make_valid_rar4_bytes()
        prefix_with_fake_rar4 = (
            mp4_hdr
            + b"\x33" * 80
            + b"Rar!\x1a\x07\x00" + b"\x00\x00\x73\x00\x00\x0d\x00" + b"\xaa" * 20  # 错 CRC16
            + b"\x44" * 80
        )
        real_rar4_mp4 = self.scratch / "fake_then_real_rar4.mp4"
        real_rar4_mp4.write_bytes(prefix_with_fake_rar4 + valid_rar4)
        self.assertEqual(
            detect_embedded_archive(real_rar4_mp4),
            (".rar", len(prefix_with_fake_rar4)),
        )

        # 4) 假 RAR5 后紧跟合法 RAR5 (Main=1 与 Encryption=4 均验证)
        for rar5_type in (1, 4):
            valid_rar5 = _make_valid_rar5_bytes(head_type=rar5_type)
            prefix_with_fake_rar5 = (
                mp4_hdr
                + b"\x55" * 64
                + b"Rar!\x1a\x07\x01\x00" + b"\x11\x22\x33\x44\x03\x01\x00\x00"  # 错 CRC32
                + b"\x66" * 64
            )
            real_rar5_mp4 = self.scratch / f"fake_then_real_rar5_t{rar5_type}.mp4"
            real_rar5_mp4.write_bytes(prefix_with_fake_rar5 + valid_rar5)
            self.assertEqual(
                detect_embedded_archive(real_rar5_mp4),
                (".rar", len(prefix_with_fake_rar5)),
            )

        # 5) 偏移 0 保持宽容（头部受损待修复包仍可识别）
        damaged_offset0_7z = self.scratch / "damaged_0.7z"
        damaged_offset0_7z.write_bytes(b"\x37\x7a\xbc\xaf\x27\x1c" + b"\x00" * 16)
        self.assertEqual(detect_embedded_archive(damaged_offset0_7z), (".7z", 0))

    def test_scene_2_large_carrier_mp4_zip_eocd_and_apk_whitelist(self):
        # 模拟 MP4 载体 (4096B) > max_scan_bytes (1024B)，且 ZIP 体积 (~12KB) > 扫尾窗 (512B)
        # 同时在 MP4 内及文件尾附加假 7z 魔数干扰
        mp4_carrier = (
            b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2mp41"
            + b"\x77" * 2000
            + b"\x37\x7a\xbc\xaf\x27\x1c" + b"\xab\xcd" * 32
            + b"\x88" * 2000
        )
        exact_offset = len(mp4_carrier)
        zip_payload = _make_zip_bytes(
            {"big_inner.bin": b"Z" * 12288, "note.txt": b"eocd exact offset ok"},
            self.scratch,
        )
        trailing_fake_7z = b"\x37\x7a\xbc\xaf\x27\x1c" + b"\x13\x37" * 16

        poly_mp4 = self.scratch / "huge_carrier_poly.mp4"
        poly_mp4.write_bytes(mp4_carrier + zip_payload + trailing_fake_7z)

        with patch.object(sandbox_mod, "_EMBEDDED_TAIL_SCAN_BYTES", 512):
            ext, offset = detect_embedded_archive(poly_mp4, max_scan_bytes=1024)
        self.assertEqual(ext, ".zip")
        self.assertEqual(offset, exact_offset)

        # 受保护终点白名单 (.apk / .exe / .msi) 即使含合法 ZIP 也绝不被 EOCD 误放行
        for protected_name in ("app.apk", "setup.exe", "pkg.msi"):
            p = self.scratch / protected_name
            p.write_bytes(zip_payload)
            self.assertEqual(detect_embedded_archive(p), (None, -1))
            self.assertIsNone(detect_archive_type(p))

    def test_scene_2b_eocd_at_window_offset_zero_with_internal_fake_signature(self):
        # 构造真实 EOCD 恰好位于 65536+22 尾窗偏移 0，且 EOCD 内部偏移 +4 含伪 PK\x05\x06 字节：
        # 验证 rfind 下界 >= 4 不会提前退出漏检真实 EOCD
        mp4_prefix = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2mp41" + b"\x55" * 1024
        tmp_zip = self.scratch / "max_comment.zip"
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_STORED) as zf:
            zf.writestr("inner.txt", b"window zero eocd ok")
            zf.comment = b"C" * 65535
        raw_zip = bytearray(tmp_zip.read_bytes())
        tmp_zip.unlink()

        eocd_rel = raw_zip.rfind(b"PK\x05\x06")
        self.assertNotEqual(eocd_rel, -1)
        # 在 EOCD 的 disk_num/cd_disk 字段 (+4..+8) 注入伪 PK\x05\x06（总长 22 + 65535 + 1 = 65558）
        raw_zip[eocd_rel + 4 : eocd_rel + 8] = b"PK\x05\x06"
        poly = self.scratch / "window_zero_eocd.mp4"
        poly.write_bytes(mp4_prefix + bytes(raw_zip) + b"\x00")

        ext, offset = detect_embedded_archive(poly, max_scan_bytes=512)
        self.assertEqual(ext, ".zip")
        self.assertEqual(offset, len(mp4_prefix))


# ==================== 场景 3：伪装包链 ② 硬链接零拷贝不短路 ====================


class _DisguisedZipOnlyEngine(BaseEngine):
    """仅接受 .zip 后缀才成功提取的模拟引擎（用于验证链 ① 失败后链 ② 硬链接改名 .zip）。"""

    def __init__(self, exe_path: Path):
        super().__init__(exe_path)
        self.extract_calls: list[str] = []
        self.repair_calls: list[str] = []

    def extract(self, archive, dest, password=None, timeout=300, task_id=None, on_progress=None) -> Path:
        p = Path(archive)
        self.extract_calls.append(p.name)
        if p.suffix.lower() != ".zip":
            raise ExtractionFailedError(str(p), 2, f"unsupported extension: {p.suffix}")
        dest_p = Path(dest)
        dest_p.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(p, "r") as zf:
            zf.extractall(dest_p)
        if on_progress:
            on_progress(100)
        return dest_p

    def repair_extract(self, archive, dest, password=None, timeout=600, task_id=None, on_progress=None) -> Path:
        self.repair_calls.append(Path(archive).name)
        raise ExtractionFailedError(str(archive), 2, "repair failed")


class TestV121DisguisedFallbackAndHardlink(_IsolatedEnvMixin, unittest.TestCase):
    def setUp(self):
        self._setup_env("sb_v121_pipe_")
        self.fake_exe = self.scratch / "mock_engine.exe"
        self.fake_exe.write_bytes(b"MZ")
        self.out_dir = self.scratch / "delivered"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.set("output_mode", "custom")
        self.cfg.set("custom_output_path", str(self.out_dir))
        self.cfg.set("enable_ad_filter", False)
        self.cfg.set("enable_nested_limit_confirm", False)

    def tearDown(self):
        self._teardown_env()

    def test_scene_3_disguised_mp4_chain2_hardlink_not_shortcircuited_and_single_counter(self):
        # 构造外层 .mp4（内含真实 ZIP），并模拟 prepare_file 误判将其硬链接为 sandbox/movie.7z
        mp4_src = self.scratch / "disguised_movie.mp4"
        zip_bytes = _make_zip_bytes({"hello.txt": b"chain2 hardlink success"}, self.scratch)
        mp4_src.write_bytes(zip_bytes)

        sandbox = SandboxManager()
        orig_prepare = sandbox.prepare_file

        def _misdetected_prepare(src_path, sandbox_dir):
            # 模拟外层伪装 .mp4 被误判并准备为 .7z（原本会被 BLIND_EXTRACT_EXTS 短路跳过链 ②！）
            wrong_7z = sandbox_dir / (Path(src_path).stem + ".7z")
            os.link(src_path, wrong_7z)
            return wrong_7z

        engine = _DisguisedZipOnlyEngine(self.fake_exe)
        layer_events: list[tuple[int, int, str]] = []
        pipeline = ExtractionPipeline(
            engine=engine,
            engine_name="MockDisguised",
            sandbox=sandbox,
            password_manager=self.pwd_mgr,
            config=self.cfg,
            on_layer_progress=lambda tid, pct, total, text: layer_events.append((pct, total, text)),
        )

        link_calls: list[tuple[Path, Path]] = []
        real_os_link = os.link

        def _spy_link(src, dst, *args, **kwargs):
            link_calls.append((Path(src), Path(dst)))
            return real_os_link(src, dst, *args, **kwargs)

        with patch.object(sandbox, "prepare_file", side_effect=_misdetected_prepare), \
             patch.object(pipeline_mod.os, "link", side_effect=_spy_link), \
             patch.object(pipeline_mod.shutil, "copy2", wraps=shutil.copy2) as spy_copy2, \
             patch.object(pipeline_mod.shutil, "copy", wraps=shutil.copy) as spy_copy:
            ok = pipeline.process("t_chain2_hl", str(mp4_src))

        self.assertTrue(ok)
        # 链 ① 尝试 .7z 失败 -> 链 ② 硬链接改名 *_renamed.zip 成功，未走链 ③
        self.assertEqual(engine.extract_calls[0], "disguised_movie.7z")
        self.assertTrue(engine.extract_calls[1].endswith("_renamed.zip"))
        self.assertEqual(engine.repair_calls, [])

        # 确认链 ② 使用了 os.link 同盘零拷贝，未触发全量 copy/copy2
        renamed_links = [dst for _, dst in link_calls if dst.name.endswith("_renamed.zip")]
        self.assertEqual(len(renamed_links), 1)
        spy_copy2.assert_not_called()
        spy_copy.assert_not_called()

        # 原始 .mp4 完好无损，产物正确交付，双计数器 total 严格为 1
        self.assertTrue(mp4_src.is_file())
        self.assertEqual(mp4_src.read_bytes(), zip_bytes)
        delivered = self.out_dir / "disguised_movie" / "hello.txt"
        self.assertTrue(delivered.is_file())
        self.assertEqual(delivered.read_text(encoding="utf-8"), "chain2 hardlink success")
        self.assertEqual(max(total for _, total, _ in layer_events), 1)
        self.assertIsNotNone(orig_prepare)


# ==================== 场景 4：Bandizip CLI 统一纠偏 ====================


class TestV121BandizipCliResolution(_IsolatedEnvMixin, unittest.TestCase):
    def setUp(self):
        self._setup_env("sb_v121_bz_")

    def tearDown(self):
        self._teardown_env()

    def test_scene_4_resolve_bandizip_cli_engine_discovery_and_settings_page(self):
        # 目录 A：Bandizip.exe 同级含 bz.exe
        dir_with_bz = self.scratch / "Bandizip_With_BZ"
        dir_with_bz.mkdir()
        gui_exe = dir_with_bz / "Bandizip.exe"
        cli_bz = dir_with_bz / "bz.exe"
        gui_exe.write_bytes(b"gui")
        cli_bz.write_bytes(b"cli")

        # 目录 B：Bandizip.exe 仅在一层子目录含 bc.exe
        dir_sub_bc = self.scratch / "Bandizip_Sub_BC"
        (dir_sub_bc / "cli").mkdir(parents=True)
        gui_sub = dir_sub_bc / "Bandizip.EXE"
        cli_bc = dir_sub_bc / "cli" / "bc.exe"
        gui_sub.write_bytes(b"gui")
        cli_bc.write_bytes(b"cli")

        # 目录 C：仅有 Bandizip.exe（无 bz.exe / bc.exe）
        dir_gui_only = self.scratch / "Bandizip_GUI_Only"
        dir_gui_only.mkdir()
        gui_only = dir_gui_only / "Bandizip.exe"
        gui_only.write_bytes(b"gui")

        # 1) resolve_bandizip_cli 单元测试
        self.assertEqual(Path(resolve_bandizip_cli(cli_bz)), cli_bz.resolve())
        self.assertEqual(Path(resolve_bandizip_cli(gui_exe)), cli_bz.resolve())
        self.assertEqual(Path(resolve_bandizip_cli(dir_with_bz)), cli_bz.resolve())
        self.assertEqual(Path(resolve_bandizip_cli(gui_sub)), cli_bc.resolve())
        self.assertIsNone(resolve_bandizip_cli(gui_only))

        # 2) BandizipEngine.__init__ 纠偏与拦截 + 旧 mock 兼容
        eng_fixed = BandizipEngine(gui_exe)
        self.assertEqual(Path(eng_fixed.exe_path), cli_bz.resolve())

        with self.assertRaises(EngineNotFoundError) as ctx:
            BandizipEngine(gui_only)
        self.assertIn("bz.exe", str(ctx.exception))

        mock_fake = self.scratch / "fake_bz.exe"
        mock_fake.write_bytes(b"mock")
        eng_mock = BandizipEngine(mock_fake)
        self.assertEqual(Path(eng_mock.exe_path), mock_fake.resolve())

        # 3) discover_engine 纠偏与无 bz.exe 时优雅降级
        self.cfg.set("default_engine", "bandizip")
        self.cfg.set("bandizip_path", str(gui_exe))
        etype, exe = discover_engine(self.cfg)
        self.assertEqual(etype, EngineType.BANDIZIP)
        self.assertEqual(Path(exe), cli_bz.resolve())

        fake_7z = self.scratch / "7z.exe"
        fake_7z.write_bytes(b"7z")
        self.cfg.set("bandizip_path", str(gui_only))
        self.cfg.set("winrar_path", "")
        self.cfg.set("sevenzip_path", str(fake_7z))
        with patch.object(engine_mod, "_find_bandizip_cli", return_value=None), \
             patch.object(engine_mod, "_find_winrar_cli", return_value=None):
            etype_deg, exe_deg = discover_engine(self.cfg)
        self.assertEqual(etype_deg, EngineType.SEVENZIP)
        self.assertEqual(Path(exe_deg).resolve(), fake_7z.resolve())

        # 4) SettingsPage 离屏校验：选 Bandizip.exe 自动回填 bz.exe；无 bz.exe 拒存并报错
        self.cfg.set("bandizip_path", "")
        page = SettingsPage()
        try:
            page.picker_bandizip.set_path(str(gui_exe))
            page.picker_bandizip.path_changed.emit(str(gui_exe))
            self.assertEqual(Path(page.picker_bandizip.path()), cli_bz.resolve())
            self.assertEqual(Path(self.cfg.get("bandizip_path")), cli_bz.resolve())
            self.assertEqual(page.row_bandizip.error_label.text(), "")

            # 选择无 bz.exe 的 Bandizip.exe -> 不覆盖正确配置，显示错误提示
            page.picker_bandizip.set_path(str(gui_only))
            page.picker_bandizip.path_changed.emit(str(gui_only))
            self.assertEqual(Path(self.cfg.get("bandizip_path")), cli_bz.resolve())
            self.assertIn("bz.exe", page.row_bandizip.error_label.text())
            self.assertFalse(page.flush())

            # 旧配置中遗留 Bandizip.exe 时，reload_from_config 回显自动规范化为同目录 bz.exe
            self.cfg.set("bandizip_path", str(gui_exe))
            page.reload_from_config()
            self.assertEqual(Path(page.picker_bandizip.path()), cli_bz.resolve())
        finally:
            page.close()


# ==================== 场景 5：WinRAR repair 守卫、exit=10 与图种预检容错 ====================


class TestV121WinRARRepairExit10AndPolyglotPrecheck(_IsolatedEnvMixin, unittest.TestCase):
    def setUp(self):
        self._setup_env("sb_v121_wr_")
        self.wr_exe = self.scratch / "WinRAR.exe"
        self.wr_exe.write_bytes(b"MZ")
        self.sz_exe = self.scratch / "7z.exe"
        self.sz_exe.write_bytes(b"MZ")
        self.bz_exe = self.scratch / "bz.exe"
        self.bz_exe.write_bytes(b"MZ")

    def tearDown(self):
        self._teardown_env()

    def test_scene_5_winrar_repair_guard_progress_and_exit10_encrypted_zip(self):
        engine = WinRAREngine(self.wr_exe)
        sz_engine = SevenZipEngine(self.sz_exe)

        # 1) supports_repair_for 守卫：WinRAR 仅支持 .rar/.zip，拒修 .7z；7-Zip 恒 False
        sample_7z = self.scratch / "huge.7z"
        sample_7z.write_bytes(_make_valid_7z_bytes(32))
        sample_rar = self.scratch / "broken.rar"
        sample_rar.write_bytes(_make_valid_rar4_bytes())
        sample_zip = self.scratch / "broken.zip"
        sample_zip.write_bytes(_make_zip_bytes({"a.txt": b"hi"}, self.scratch))

        self.assertFalse(engine.supports_repair_for(sample_7z))
        self.assertTrue(engine.supports_repair_for(sample_rar))
        self.assertTrue(engine.supports_repair_for(sample_zip))
        self.assertFalse(sz_engine.supports_repair_for(sample_zip))

        # 2) WinRAR.repair_extract 面对 .7z 直接拒跑 WinRAR r（毫秒级失败）
        with patch.object(engine, "_run_command") as mock_run:
            with self.assertRaises(ExtractionFailedError):
                engine.repair_extract(sample_7z, self.scratch / "out_7z")
            mock_run.assert_not_called()

        # 3) WinRAR.repair_extract 面对 .rar：开跑前上报 on_progress(-1)；无修复产物不重跑 extract
        pcts: list[int] = []
        with patch.object(
            engine, "_run_command", return_value=subprocess.CompletedProcess(["WinRAR", "r"], 0, "", "")
        ) as mock_run, patch.object(engine, "extract") as mock_extract:
            with self.assertRaises(ExtractionFailedError):
                engine.repair_extract(sample_rar, self.scratch / "out_rar", on_progress=pcts.append)
            mock_run.assert_called_once()
            mock_extract.assert_not_called()
        self.assertEqual(pcts, [-1])

        # 4) 退出码 10 精准判别：真加密 ZIP -> PasswordRequiredError；损坏假 ZIP -> ExtractionFailedError（防盲跑密码库）
        enc_zip = self.scratch / "encrypted.zip"
        enc_zip.write_bytes(_make_encrypted_flag_zip_bytes(self.scratch))
        self.assertTrue(_is_encrypted_zip(enc_zip))

        # MP4+ZIP 加密图种同样被 _is_encrypted_zip 精准识别
        enc_poly = self.scratch / "encrypted_poly.mp4"
        enc_poly.write_bytes(
            b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2mp41" + b"\x00" * 512
            + _make_encrypted_flag_zip_bytes(self.scratch)
        )
        self.assertTrue(_is_encrypted_zip(enc_poly))

        corrupt_zip = self.scratch / "corrupt_fake.zip"
        corrupt_zip.write_bytes(b"PK\x03\x04NOT_A_REAL_ZIP_CORRUPTED_DATA")
        self.assertFalse(_is_encrypted_zip(corrupt_zip))

        rc10 = subprocess.CompletedProcess(["WinRAR", "x"], 10, "", "No files to extract")
        with patch.object(engine, "_run_command", return_value=rc10):
            with self.assertRaises(PasswordRequiredError):
                engine.extract(enc_zip, self.scratch / "out_enc", password=None)
            with self.assertRaises(PasswordRequiredError):
                engine.extract(enc_poly, self.scratch / "out_poly", password=None)
            with self.assertRaises(ExtractionFailedError):
                engine.extract(corrupt_zip, self.scratch / "out_corrupt", password=None)

        # 5) 退出码 10 + pw_provided=True：真加密 ZIP -> WrongPasswordError；损坏假 ZIP -> ExtractionFailedError
        with patch.object(engine, "_run_command", return_value=rc10):
            with self.assertRaises(WrongPasswordError) as wp_ctx:
                engine.extract(enc_zip, self.scratch / "out_enc_wp", password="wrong_pwd")
            self.assertEqual(wp_ctx.exception.password_used, "wrong_pwd")
            with self.assertRaises(WrongPasswordError):
                engine.extract(enc_poly, self.scratch / "out_poly_wp", password="wrong_pwd")
            with self.assertRaises(ExtractionFailedError):
                engine.extract(corrupt_zip, self.scratch / "out_corrupt_wp", password="wrong_pwd")

        # 6) 端到端管线验证：WinRAR 对加密 ZIP 错密返回 exit=10 -> 正确触发密码重试，绝不误入链 ③ repair
        out_dir = self.scratch / "delivered_rc10"
        out_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.set("output_mode", "custom")
        self.cfg.set("custom_output_path", str(out_dir))
        self.cfg.set("enable_ad_filter", False)
        self.cfg.set("enable_nested_limit_confirm", False)
        self.pwd_mgr.add("vault_bad", tag="test")

        wrong_notified: list[str] = []
        manual_pwds = iter(["user_bad", "user_good"])
        repair_called = False

        def _fake_wr_pipeline_run(cmd, timeout=300, task_id=None, **kwargs):
            if "-puser_good" in cmd:
                dest_arg = Path(cmd[-1])
                dest_arg.mkdir(parents=True, exist_ok=True)
                (dest_arg / "unlocked.txt").write_text("rc10 retry ok", encoding="utf-8")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            # 无密 (-p-) 与错密 (-pvault_bad / -puser_bad) 均返回 exit=10
            return subprocess.CompletedProcess(cmd, 10, "", "No files to extract")

        def _forbid_repair(*args, **kwargs):
            nonlocal repair_called
            repair_called = True
            raise AssertionError("加密 ZIP 错密返回 exit=10 绝不能触发 repair_extract")

        pipe = ExtractionPipeline(
            engine=engine,
            engine_name="WinRAR",
            sandbox=SandboxManager(),
            password_manager=self.pwd_mgr,
            config=self.cfg,
            on_password_wrong=wrong_notified.append,
            wait_for_password=lambda tid: next(manual_pwds, None),
        )
        with patch.object(engine, "_run_command", side_effect=_fake_wr_pipeline_run), \
             patch.object(engine, "repair_extract", side_effect=_forbid_repair):
            ok_retry = pipe.process("t_rc10_retry", str(enc_zip))

        self.assertTrue(ok_retry)
        self.assertFalse(repair_called)
        self.assertEqual(wrong_notified, ["t_rc10_retry"])
        self.assertEqual(
            (out_dir / "encrypted" / "unlocked.txt").read_text(encoding="utf-8"),
            "rc10 retry ok",
        )

        # 7) 退出码 12/13 显式纳管为 ExtractionFailedError
        for code in (12, 13):
            res_code = subprocess.CompletedProcess(["WinRAR", "x"], code, "", f"err {code}")
            with self.assertRaises(ExtractionFailedError) as err_ctx:
                WinRAREngine._classify_exit_code(res_code, str(corrupt_zip), pw_provided=False)
            self.assertEqual(err_ctx.exception.returncode, code)

    def test_scene_5b_test_password_polyglot_resilience_across_three_engines(self):
        # 构造合法 MP4+加密ZIP 图种
        mp4_hdr = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2mp41" + b"\x00" * 256
        poly_zip = self.scratch / "poly_enc.zip"
        poly_zip.write_bytes(mp4_hdr + _make_encrypted_flag_zip_bytes(self.scratch))

        # 1) SevenZipEngine.test_password: l -slt / t 无 -tzip 报 Headers Error -> 自动附加 -tzip 重试
        sz = SevenZipEngine(self.sz_exe)
        sz_cmds: list[list[str]] = []

        def _fake_sz(cmd, timeout=60, task_id=None, **kwargs):
            sz_cmds.append(list(cmd))
            if "-tzip" not in cmd:
                return subprocess.CompletedProcess(cmd, 2, "", "ERROR: Headers Error")
            if cmd[1] == "l":
                return subprocess.CompletedProcess(
                    cmd, 0, "----------\nPath = secret.txt\nFolder = -\nSize = 23\n", ""
                )
            if "-pgood" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "Everything is Ok", "")
            return subprocess.CompletedProcess(cmd, 2, "", "ERROR: Wrong password")

        with patch.object(sz, "_run_command", side_effect=_fake_sz):
            self.assertFalse(sz.test_password(poly_zip, "wrong"))
            self.assertTrue(sz.test_password(poly_zip, "good"))
        self.assertTrue(any("-tzip" in c and c[1] == "l" for c in sz_cmds))
        self.assertTrue(any("-tzip" in c and c[1] == "t" for c in sz_cmds))

        # 2) WinRAREngine.test_password: 加密 ZIP 的 lt 返回 10 或 11 时绝不误标加密头走 lb，而是走 t
        wr = WinRAREngine(self.wr_exe)
        wr_cmds: list[list[str]] = []

        def _fake_wr(cmd, timeout=60, task_id=None, **kwargs):
            wr_cmds.append(list(cmd))
            if cmd[1] == "lt":
                return subprocess.CompletedProcess(cmd, 10, "", "")
            if cmd[1] == "t" and "-pwrong" in cmd:
                return subprocess.CompletedProcess(cmd, 10, "", "")
            if cmd[1] == "t" and "-pgood" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "", "")
            raise AssertionError(f"Unexpected WinRAR cmd: {cmd}")

        with patch.object(wr, "_run_command", side_effect=_fake_wr):
            self.assertFalse(wr.test_password(poly_zip, "wrong"))
            self.assertTrue(wr.test_password(poly_zip, "good"))
        self.assertFalse(any(c[1] == "lb" for c in wr_cmds))

        # 3) BandizipEngine.test_password: 偏移图种 l 列表失败时平滑降级为整包 t
        bz = BandizipEngine(self.bz_exe)
        bz_cmds: list[list[str]] = []

        def _fake_bz(cmd, timeout=60, task_id=None, **kwargs):
            bz_cmds.append(list(cmd))
            if cmd[1] == "l":
                return subprocess.CompletedProcess(cmd, 2, "", "Can't open archive")
            if cmd[1] == "t" and "-p:wrong" in cmd:
                return subprocess.CompletedProcess(cmd, 2, "", "Wrong password")
            if cmd[1] == "t" and "-p:good" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "Everything is Ok", "")
            raise AssertionError(f"Unexpected Bandizip cmd: {cmd}")

        with patch.object(bz, "_run_command", side_effect=_fake_bz):
            self.assertFalse(bz.test_password(poly_zip, "wrong"))
            self.assertTrue(bz.test_password(poly_zip, "good"))
        self.assertTrue(any(c[1] == "t" for c in bz_cmds))


# ==================== 场景 6：真实 assets/bin/7z.exe 大偏移加密 MP4+ZIP 端到端 ====================


class TestV121RealSevenZipEncryptedPolyglotE2E(_IsolatedEnvMixin, unittest.TestCase):
    def setUp(self):
        self._setup_env("sb_v121_e2e_")
        self.out_dir = self.scratch / "delivered"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.set("output_mode", "custom")
        self.cfg.set("custom_output_path", str(self.out_dir))
        self.cfg.set("enable_ad_filter", False)
        self.cfg.set("enable_nested_limit_confirm", False)

    def tearDown(self):
        self._teardown_env()

    def test_scene_6_real_7z_large_offset_encrypted_mp4_zip_polyglot(self):
        bundled_7z = PROJECT_ROOT / "assets" / "bin" / "7z.exe"
        if not bundled_7z.is_file():
            self.skipTest("assets/bin/7z.exe 不存在，跳过真实 7-Zip 加密图种端到端测试")

        # 1) 使用真实 7z.exe 创建密码加密的 ZIP 包
        payload_src = self.scratch / "secret_doc.txt"
        payload_text = "ShellBreaker v1.2.1 Encrypted MP4+ZIP Polyglot OK!"
        payload_src.write_text(payload_text, encoding="utf-8")

        raw_enc_zip = self.scratch / "inner_encrypted.zip"
        password = "PolySecret2026!"
        res = subprocess.run(
            [str(bundled_7z), "a", "-tzip", f"-p{password}", "-y", str(raw_enc_zip), str(payload_src)],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.assertEqual(res.returncode, 0, f"创建加密 ZIP 失败: {res.stderr}")
        enc_zip_bytes = raw_enc_zip.read_bytes()
        raw_enc_zip.unlink()

        # 2) 构造 >2MB 的 MP4 载体（中间故意掺入裸假 7z 魔数）+ 真实加密 ZIP 构成大偏移加密图种
        mp4_carrier = (
            b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2mp41"
            + b"\x41" * (1024 * 1024)
            + b"\x37\x7a\xbc\xaf\x27\x1c" + b"\xde\xad\xbe\xef" * 16  # 假 7z 干扰
            + b"\x42" * (1024 * 1024)
        )
        poly_mp4 = self.scratch / "demo_encrypted_video.mp4"
        poly_mp4.write_bytes(mp4_carrier + enc_zip_bytes)

        # 验证图种嗅探在极小 max_scan_bytes 下仍通过 O(1) EOCD 精准命中 .zip 且精确反算偏移
        ext, offset = detect_embedded_archive(poly_mp4, max_scan_bytes=1024)
        self.assertEqual(ext, ".zip")
        self.assertEqual(offset, len(mp4_carrier))
        self.assertTrue(_is_encrypted_zip(poly_mp4))

        # 3) 密码库置入若干错误密码 + 正确密码，走完整 ExtractionPipeline（强制触发快速预检路径验证兼容性）
        for wrong in ("wrong1", "wrong2", "wrong3"):
            self.pwd_mgr.add(wrong, tag="test")
        self.pwd_mgr.add(password, tag="test")

        engine = SevenZipEngine(bundled_7z)
        layer_events: list[tuple[int, int, str]] = []
        pipeline = ExtractionPipeline(
            engine=engine,
            engine_name="7-Zip",
            sandbox=SandboxManager(),
            password_manager=self.pwd_mgr,
            config=self.cfg,
            on_layer_progress=lambda tid, pct, total, text: layer_events.append((pct, total, text)),
        )

        with patch.object(pipeline_mod, "FAST_PRECHECK_MIN_SIZE_BYTES", 1024):
            ok = pipeline.process("t_real_7z_poly", str(poly_mp4))

        self.assertTrue(ok)
        delivered = self.out_dir / "demo_encrypted_video" / "secret_doc.txt"
        self.assertTrue(delivered.is_file(), f"解密交付产物缺失: {delivered}")
        self.assertEqual(delivered.read_text(encoding="utf-8"), payload_text)
        self.assertTrue(poly_mp4.is_file())
        self.assertEqual(max(total for _, total, _ in layer_events), 1)


if __name__ == "__main__":
    unittest.main()
