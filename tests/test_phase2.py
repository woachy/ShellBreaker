"""
Batch 2 (Phase 2 / P1) 完整单元测试与跨模块集成测试套件。

全套测试支持 QT_QPA_PLATFORM=offscreen 离屏运行，零外部 Bandizip / WinRAR 依赖。
覆盖范围：
1. 同盘隐藏沙箱与零拷贝交付（get_sandbox_dir_for_target / cleanup_sandbox / clean_all / deliver_to_target）
2. 伪装重命名事务日志与断电自愈（record/remove/recover_interrupted_renames + Pipeline 集成）
3. read_text_safe 编码决胜与上下文密码嗅探（UTF-8 BOM / GB18030 vs Big5 / PUA 惩罚 / 四级嗅探 / record_success / Pipeline 集成）
4. 大包多密快速预检（BandizipEngine & WinRAREngine test_password + ExtractionPipeline 集成）
"""
import ctypes
import errno
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.pipeline as pipeline_mod
import core.sandbox as sandbox_mod
import data.config_manager as cfg_mod
from core.engine import BandizipEngine, BaseEngine, WinRAREngine
from core.exceptions import (
    ExtractionAbortedError,
    ExtractionFailedError,
    PasswordRequiredError,
    WrongPasswordError,
)
from core.pipeline import ExtractionPipeline
from core.sandbox import (
    FILE_ATTRIBUTE_HIDDEN,
    SANDBOX_CONTAINER_NAME,
    SandboxManager,
    get_sandbox_dir_for_target,
    record_rename_journal,
    recover_interrupted_renames,
    remove_rename_journal,
)
from data.config_manager import DEFAULT_CONFIG, ConfigManager
from data.password_manager import (
    PasswordManager,
    _score_decoded_text,
    extract_contextual_passwords,
    read_text_safe,
)


# ==================== 通用辅助：隔离固件与离屏 Mock 引擎 ====================


def _make_isolated_config(tmp_path: Path, monkeypatch, overrides: dict | None = None) -> ConfigManager:
    """将 ConfigManager 单例隔离到 tmp_path 下的独立 config.json，避免污染真实配置。"""
    cfg_file = tmp_path / "isolated_config.json"
    data = dict(DEFAULT_CONFIG)
    if overrides:
        data.update(overrides)
    cfg_file.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    monkeypatch.setattr(cfg_mod, "CONFIG_PATH", cfg_file)
    cfg = ConfigManager()
    monkeypatch.setattr(cfg, "_data", dict(data))
    monkeypatch.setattr(cfg, "_loaded", True)
    return cfg


def _make_isolated_pwd_manager(tmp_path: Path, monkeypatch, initial_items: list[dict] | None = None) -> PasswordManager:
    """将 PasswordManager 单例隔离到 tmp_path 下的独立 passwords.json，避免污染真实密码库。"""
    pwd_file = tmp_path / "isolated_passwords.json"
    items = list(initial_items or [])
    pwd_file.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")

    pm = PasswordManager()
    monkeypatch.setattr(pm, "_file", pwd_file)
    monkeypatch.setattr(pm, "_cache", [])
    monkeypatch.setattr(pm, "_loaded", False)
    pm.reload()
    return pm


def _create_zip_file(dest_zip: Path, files: dict[str, bytes | str]) -> Path:
    """创建包含指定文件映射的真实 ZIP 文件（纯 Python，无外部依赖）。"""
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_STORED) as zf:
        for arcname, content in files.items():
            if isinstance(content, str):
                zf.writestr(arcname, content.encode("utf-8"))
            else:
                zf.writestr(arcname, content)
    return dest_zip


class _FakeZipEngine(BaseEngine):
    """纯 Python Mock 解压引擎：支持密码映射、快检能力、调用轨迹记录与回调钩子。"""

    def __init__(
        self,
        exe_path: Path,
        *,
        supports_fast_test: bool = False,
        password_map: dict[str, str] | None = None,
        corrupt_names: set[str] | None = None,
        on_extract_start=None,
    ):
        super().__init__(exe_path)
        self.supports_fast_test = supports_fast_test
        self.password_map = dict(password_map or {})
        self.corrupt_names = set(corrupt_names or set())
        self.on_extract_start = on_extract_start
        self.test_calls: list[tuple[str, str | None]] = []
        self.extract_calls: list[tuple[str, str | None]] = []

    def test_password(self, archive, password=None, timeout=60, task_id=None) -> bool:
        archive_path = Path(archive)
        name = archive_path.name
        self.test_calls.append((name, password))
        if task_id and self.is_aborted(task_id):
            raise ExtractionAbortedError(task_id, "任务已被中止")
        if name in self.corrupt_names:
            raise ExtractionFailedError(str(archive_path), 2, "corrupt archive in test_password")
        expected = self.password_map.get(name)
        if expected is None:
            return True
        return password == expected

    def extract(self, archive, dest, password=None, timeout=300, task_id=None, on_progress=None) -> Path:
        archive_path = Path(archive)
        dest_path = Path(dest)
        dest_path.mkdir(parents=True, exist_ok=True)
        name = archive_path.name
        self.extract_calls.append((name, password))

        if self.on_extract_start is not None:
            self.on_extract_start(archive_path, dest_path, password)

        if task_id and self.is_aborted(task_id):
            raise ExtractionAbortedError(task_id, "任务已被中止")
        if name in self.corrupt_names:
            raise ExtractionFailedError(str(archive_path), 2, "corrupt archive")

        if name in self.password_map:
            expected = self.password_map[name]
            if not password:
                raise PasswordRequiredError(str(archive_path), "password required")
            if password != expected:
                raise WrongPasswordError(str(archive_path), password, "wrong password")

        if not zipfile.is_zipfile(archive_path):
            raise ExtractionFailedError(str(archive_path), 2, "not a valid zip")

        with zipfile.ZipFile(archive_path, "r") as zf:
            zf.extractall(dest_path)

        if on_progress is not None:
            on_progress(50)
            on_progress(100)
        return dest_path

    def repair_extract(self, archive, dest, password=None, timeout=600, task_id=None, on_progress=None) -> Path:
        return self.extract(archive, dest, password=password, timeout=timeout, task_id=task_id, on_progress=on_progress)


# ==================== 1. 同盘隐藏沙箱与零拷贝交付 ====================


class TestSameVolumeSandboxAndZeroCopyDelivery:
    """覆盖同盘隐藏沙箱动态路由、空容器自动清理、零拷贝交付与 EXDEV 平滑回退。"""

    def test_get_sandbox_dir_routes_dir_file_and_nonexistent_with_hidden_attr(self, tmp_path, monkeypatch):
        fallback_root = tmp_path / "default_sandbox_root"
        monkeypatch.setattr(sandbox_mod, "SANDBOX_ROOT", fallback_root)

        # 1) 正常存在目录: workspace/sub/out_dir -> workspace/sub/.shellbreaker_sandbox/task_dir
        out_dir = tmp_path / "workspace" / "sub" / "out_dir"
        out_dir.mkdir(parents=True)
        sb_dir = get_sandbox_dir_for_target(out_dir, "task_dir")
        expected_container_1 = out_dir.parent / SANDBOX_CONTAINER_NAME
        assert sb_dir == (expected_container_1 / "task_dir").resolve()
        assert sb_dir.is_dir()

        if os.name == "nt":
            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(expected_container_1))
            assert attrs != -1
            assert (attrs & FILE_ATTRIBUTE_HIDDEN) == FILE_ATTRIBUTE_HIDDEN

        # 2) 已存在文件路径: workspace/file_case/inner/pkg.zip -> workspace/file_case/.shellbreaker_sandbox/task_file
        file_parent = tmp_path / "workspace" / "file_case" / "inner"
        file_parent.mkdir(parents=True)
        sample_file = file_parent / "pkg.zip"
        sample_file.write_bytes(b"data")
        sb_file = get_sandbox_dir_for_target(sample_file, "task_file")
        expected_container_2 = file_parent.parent / SANDBOX_CONTAINER_NAME
        assert sb_file == (expected_container_2 / "task_file").resolve()
        assert sb_file.is_dir()

        # 3) 不存在路径: workspace/missing_case/inner/not_created_yet -> workspace/missing_case/.shellbreaker_sandbox/task_missing
        missing_parent = tmp_path / "workspace" / "missing_case" / "inner"
        missing_parent.mkdir(parents=True)
        nonexistent_target = missing_parent / "not_created_yet"
        sb_missing = get_sandbox_dir_for_target(nonexistent_target, "task_missing")
        expected_container_3 = missing_parent.parent / SANDBOX_CONTAINER_NAME
        assert sb_missing == (expected_container_3 / "task_missing").resolve()
        assert sb_missing.is_dir()

    def test_get_sandbox_dir_fallback_on_permission_error(self, tmp_path, monkeypatch):
        fallback_root = tmp_path / "fallback_root"
        monkeypatch.setattr(sandbox_mod, "SANDBOX_ROOT", fallback_root)

        target_dir = tmp_path / "readonly_parent" / "out"
        target_dir.mkdir(parents=True)
        real_write_bytes = Path.write_bytes

        def _deny_probe(self_path: Path, data: bytes):
            if self_path.name.startswith(".sb_write_probe_"):
                raise PermissionError("access denied on target drive")
            return real_write_bytes(self_path, data)

        monkeypatch.setattr(Path, "write_bytes", _deny_probe)

        sb = get_sandbox_dir_for_target(target_dir, "task_perm_fallback")
        assert sb == fallback_root / "task_perm_fallback"
        assert sb.is_dir()

    def test_cleanup_sandbox_and_clean_all_container_lifecycle(self, tmp_path, monkeypatch):
        fallback_root = tmp_path / "fallback_root"
        monkeypatch.setattr(sandbox_mod, "SANDBOX_ROOT", fallback_root)

        mgr = SandboxManager()
        target_dir = tmp_path / "drive_sim" / "downloads"
        target_dir.mkdir(parents=True)
        container = target_dir.parent / SANDBOX_CONTAINER_NAME

        sb1 = mgr.create_sandbox("task_alpha", target_dir=target_dir)
        sb2 = mgr.create_sandbox("task_beta", target_dir=target_dir)
        (sb1 / "dummy.txt").write_text("a", encoding="utf-8")
        (sb2 / "dummy.txt").write_text("b", encoding="utf-8")
        assert container.is_dir()

        # 清理 sb1 后，sb2 仍存在 -> 容器不得误删
        mgr.cleanup_sandbox(sb1)
        assert not sb1.exists()
        assert sb2.exists()
        assert container.is_dir()

        # 清理 sb2 后，容器已空 -> 自动删除空容器
        mgr.cleanup_sandbox(sb2)
        assert not sb2.exists()
        assert not container.exists()

        # clean_all：清理所有追踪沙箱并回收空容器
        sb3 = mgr.create_sandbox("task_gamma", target_dir=target_dir)
        sb4 = mgr.create_sandbox("task_delta", target_dir=target_dir)
        assert container.is_dir()
        removed = mgr.clean_all()
        assert removed == 2
        assert not sb3.exists()
        assert not sb4.exists()
        assert not container.exists()

    def test_deliver_to_target_zero_copy_and_exdev_fallback_with_unique_names(self, tmp_path, monkeypatch):
        _make_isolated_config(tmp_path, monkeypatch, {"output_mode": "source_directory", "custom_output_path": ""})

        src_dir = tmp_path / "source_area"
        src_dir.mkdir()
        src_archive = src_dir / "game_pack.zip"
        src_archive.write_bytes(b"zip")

        # 1) 同盘正常 os.replace 零拷贝交付
        sb1 = tmp_path / "sb1"
        out1 = sb1 / "output"
        (out1 / "assets").mkdir(parents=True)
        (out1 / "readme.txt").write_text("hello zero copy", encoding="utf-8")
        (out1 / "assets" / "logo.bin").write_bytes(b"\x01\x02\x03")

        replace_calls: list[tuple[str, str]] = []
        real_replace = os.replace

        def _spy_replace(src, dst):
            replace_calls.append((str(src), str(dst)))
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", _spy_replace)
        delivered_1 = Path(SandboxManager.deliver_to_target(src_archive, sb1))

        assert delivered_1 == (src_dir / "game_pack").resolve()
        assert (delivered_1 / "readme.txt").read_text(encoding="utf-8") == "hello zero copy"
        assert (delivered_1 / "assets" / "logo.bin").read_bytes() == b"\x01\x02\x03"
        assert len(replace_calls) == 2

        # 2) 模拟跨盘 OSError(EXDEV)：平滑回退 shutil.move，并保留重名避让 (game_pack_1)
        sb2 = tmp_path / "sb2"
        out2 = sb2 / "output"
        (out2 / "nested_dir").mkdir(parents=True)
        (out2 / "data.txt").write_text("cross device fallback", encoding="utf-8")
        (out2 / "nested_dir" / "item.txt").write_text("nested ok", encoding="utf-8")

        move_calls: list[tuple[str, str]] = []
        real_move = shutil.move

        def _raise_exdev(src, dst):
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        def _spy_move(src, dst, *args, **kwargs):
            move_calls.append((str(src), str(dst)))
            return real_move(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, "replace", _raise_exdev)
        monkeypatch.setattr(shutil, "move", _spy_move)

        delivered_2 = Path(SandboxManager.deliver_to_target(src_archive, sb2))
        assert delivered_2 == (src_dir / "game_pack_1").resolve()
        assert (delivered_2 / "data.txt").read_text(encoding="utf-8") == "cross device fallback"
        assert (delivered_2 / "nested_dir" / "item.txt").read_text(encoding="utf-8") == "nested ok"
        assert len(move_calls) == 2


# ==================== 2. 伪装重命名事务日志与断电自愈 ====================


class TestRenameJournalAndCrashRecovery:
    """覆盖伪装重命名事务日志原子读写、PID 存活判断、断电恢复及 Pipeline 集成。"""

    def test_record_and_remove_rename_journal_chinese_paths(self, tmp_path):
        jpath = tmp_path / "rename_journal.json"
        orig_1 = tmp_path / "中文目录" / "伪装资源_01.jpg"
        temp_1 = tmp_path / "中文目录" / "伪装资源_01.zip"
        orig_2 = tmp_path / "中文目录" / "图种_02.png"
        temp_2 = tmp_path / "中文目录" / "图种_02.zip"

        record_rename_journal("task_cn_1", orig_1, temp_1, journal_path=jpath)
        record_rename_journal("task_cn_2", orig_2, temp_2, journal_path=jpath)

        raw_text = jpath.read_text(encoding="utf-8")
        assert "伪装资源_01.jpg" in raw_text  # ensure_ascii=False 验证
        entries = json.loads(raw_text)
        assert len(entries) == 2
        assert entries[0]["task_id"] == "task_cn_1"
        assert entries[0]["pid"] == os.getpid()

        # 精准移除 task_cn_1，保留 task_cn_2
        remove_rename_journal("task_cn_1", temp_path=temp_1, journal_path=jpath)
        remaining = json.loads(jpath.read_text(encoding="utf-8"))
        assert len(remaining) == 1
        assert remaining[0]["task_id"] == "task_cn_2"

    def test_recover_interrupted_renames_dead_pid_restores_original(self, tmp_path):
        jpath = tmp_path / "rename_journal.json"
        orig = tmp_path / "电影资源.mp4"
        temp = tmp_path / "电影资源.zip"
        temp.write_bytes(b"disguised archive payload")

        # 构造已退出进程的真实死亡 PID
        dead_proc = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_pid = dead_proc.pid
        dead_proc.wait(timeout=10)

        jpath.write_text(
            json.dumps(
                [
                    {
                        "task_id": "crashed_task",
                        "original_path": str(orig),
                        "temp_path": str(temp),
                        "pid": dead_pid,
                        "timestamp": 1700000000.0,
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        recovered = recover_interrupted_renames(journal_path=jpath, force=False)
        assert recovered == 1
        assert orig.exists()
        assert not temp.exists()
        assert orig.read_bytes() == b"disguised archive payload"
        assert json.loads(jpath.read_text(encoding="utf-8")) == []

    def test_recover_interrupted_renames_alive_pid_force_false_and_true(self, tmp_path):
        jpath = tmp_path / "rename_journal.json"
        orig = tmp_path / "存活测试.jpg"
        temp = tmp_path / "存活测试.zip"
        temp.write_bytes(b"live process payload")

        # 启动真实存活子进程以端到端验证 Windows _is_pid_alive
        live_proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            jpath.write_text(
                json.dumps(
                    [
                        {
                            "task_id": "live_task",
                            "original_path": str(orig),
                            "temp_path": str(temp),
                            "pid": live_proc.pid,
                            "timestamp": 1700000000.0,
                        }
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            # force=False：跳过存活 PID 并保留日志
            assert recover_interrupted_renames(journal_path=jpath, force=False) == 0
            assert temp.exists()
            assert not orig.exists()
            assert len(json.loads(jpath.read_text(encoding="utf-8"))) == 1

            # force=True：强制恢复并清理日志
            assert recover_interrupted_renames(journal_path=jpath, force=True) == 1
            assert orig.exists()
            assert not temp.exists()
            assert orig.read_bytes() == b"live process payload"
            assert json.loads(jpath.read_text(encoding="utf-8")) == []
        finally:
            live_proc.kill()
            live_proc.wait(timeout=5)

    def test_recover_interrupted_renames_corrupt_and_missing_journal(self, tmp_path):
        # 1) 不存在日志文件
        missing = tmp_path / "does_not_exist.json"
        assert recover_interrupted_renames(journal_path=missing) == 0

        # 2) 损坏 JSON
        corrupt = tmp_path / "corrupt.json"
        corrupt.write_text("{invalid json [[[", encoding="utf-8")
        assert recover_interrupted_renames(journal_path=corrupt) == 0

        # 3) 非列表结构 JSON
        non_list = tmp_path / "non_list.json"
        non_list.write_text('{"task_id": "x"}', encoding="utf-8")
        assert recover_interrupted_renames(journal_path=non_list) == 0

    def test_pipeline_disguised_archive_journal_lifecycle_success_and_rollback(self, tmp_path, monkeypatch):
        jpath = tmp_path / "pipeline_journal.json"
        monkeypatch.setattr(sandbox_mod, "RENAME_JOURNAL_PATH", jpath)
        cfg = _make_isolated_config(tmp_path, monkeypatch, {"min_file_size_mb": 0})
        pm = _make_isolated_pwd_manager(tmp_path, monkeypatch)

        dummy_exe = tmp_path / "bz.exe"
        dummy_exe.write_bytes(b"exe")

        events: list[tuple[str, int]] = []
        real_replace = os.replace

        def _spy_replace(src, dst):
            # 检查 os.replace 前事务日志是否已落盘（忽略 journal 自身的原子写盘）
            if Path(src).suffix.lower() == ".jpg" and Path(dst).suffix.lower() == ".zip":
                entries = json.loads(jpath.read_text(encoding="utf-8")) if jpath.exists() else []
                events.append(("before_replace", len(entries)))
            return real_replace(src, dst)

        def _on_extract(archive_path, dest_path, password):
            entries = json.loads(jpath.read_text(encoding="utf-8")) if jpath.exists() else []
            events.append(("during_extract", len(entries)))

        monkeypatch.setattr(os, "replace", _spy_replace)
        engine = _FakeZipEngine(dummy_exe, on_extract_start=_on_extract)
        pipeline = ExtractionPipeline(engine=engine, engine_name="Fake", password_manager=pm, config=cfg)

        # --- 分支 A：试探成功（伪装 .jpg 内是合法 zip）---
        base_ok = tmp_path / "case_ok"
        base_ok.mkdir()
        disguised_ok = base_ok / "伪装图种.jpg"
        _create_zip_file(disguised_ok, {"real_doc.txt": "extracted ok"})

        ok = pipeline.find_and_convert_disguised_archive(base_ok, set(), task_id="t_ok", passwords=[])
        assert ok is True
        assert ("before_replace", 1) in events
        assert ("during_extract", 1) in events
        assert (base_ok / "real_doc.txt").read_text(encoding="utf-8") == "extracted ok"
        assert not disguised_ok.exists()
        assert json.loads(jpath.read_text(encoding="utf-8")) == []

        # --- 分支 B：试探失败回滚（真图片非压缩包）---
        events.clear()
        base_fail = tmp_path / "case_fail"
        base_fail.mkdir()
        real_jpg = base_fail / "普通照片.jpg"
        real_jpg.write_bytes(b"\xff\xd8\xff\xe0not a zip at all")
        skipped: set[Path] = set()

        fail = pipeline.find_and_convert_disguised_archive(base_fail, skipped, task_id="t_fail", passwords=[])
        assert fail is False
        assert ("before_replace", 1) in events
        assert ("during_extract", 1) in events
        assert real_jpg.exists()
        assert real_jpg in skipped
        assert json.loads(jpath.read_text(encoding="utf-8")) == []


# ==================== 3. read_text_safe 编码决胜与上下文密码嗅探 ====================


class TestEncodingAndContextualPasswords:
    """覆盖 read_text_safe 编码识别、四级上下文密码嗅探、record_success 与 Pipeline 集成。"""

    def test_read_text_safe_utf8_bom_and_utf8(self, tmp_path):
        bom_file = tmp_path / "bom.txt"
        bom_file.write_bytes(b"\xef\xbb\xbf" + "解压密码：BOM中文测试".encode("utf-8"))
        decoded_bom = read_text_safe(bom_file)
        assert decoded_bom == "解压密码：BOM中文测试"
        assert not decoded_bom.startswith("\ufeff")

        utf8_file = tmp_path / "utf8.txt"
        utf8_file.write_bytes("密码: PlainUTF8_密码".encode("utf-8"))
        assert read_text_safe(utf8_file) == "密码: PlainUTF8_密码"

    def test_read_text_safe_gb18030_big5_tiebreak_and_pua_penalty(self, tmp_path):
        # 1) GB18030 明确含简体关键词
        gb_file = tmp_path / "gb.txt"
        gb_text = "解压密码：简体测试666\n说明文件下载"
        gb_file.write_bytes(gb_text.encode("gb18030"))
        assert read_text_safe(gb_file) == gb_text

        # 2) Big5 明确含繁体关键词
        big5_file = tmp_path / "big5.txt"
        big5_text = "解壓密碼：繁體測試888\n說明檔案下載"
        big5_file.write_bytes(big5_text.encode("big5"))
        assert read_text_safe(big5_file) == big5_text

        # 3) GB18030 与 Big5 同分决胜：构造双编码均合法且得分完全相等的字节序列，默认选 gb18030
        tie_bytes = b"\xa4\xa4\xa4\xe5"  # GB18030 -> "ぃい", Big5 -> "中文"；下面找真正同分的双常用汉字字节
        # 精确构造：验证实际两字符都在 U+4E00..U+9FFF 且无关键词 -> 分数严格相等
        tie_sample = None
        for b1 in range(0xB0, 0xD8):
            for b2 in range(0xA1, 0xFE):
                pair = bytes([b1, b2])
                try:
                    g = pair.decode("gb18030")
                    b5 = pair.decode("big5")
                except UnicodeDecodeError:
                    continue
                if g != b5 and round(_score_decoded_text(g), 6) == round(_score_decoded_text(b5), 6):
                    tie_sample = (pair, g, b5)
                    break
            if tie_sample is not None:
                break
        assert tie_sample is not None
        pair_bytes, expected_gb, _ = tie_sample
        tie_file = tmp_path / "tie.txt"
        tie_file.write_bytes(pair_bytes)
        assert read_text_safe(tie_file) == expected_gb

        # 4) 私用区字符 (U+E000..U+F8FF) 高惩罚降权
        normal_score = _score_decoded_text("普通文本abc123")
        pua_score = _score_decoded_text("普通文本\uE001\uE002abc")
        assert pua_score < normal_score - 50.0

        # 真实双解码对比：选出一个在 Big5 下落在私用区 / 罕见区而在 GB18030 下正常的字节串
        pua_winner = None
        for b1 in range(0x81, 0xFE):
            for b2 in range(0x40, 0xFE):
                raw = bytes([b1, b2])
                try:
                    g = raw.decode("gb18030")
                    b5 = raw.decode("big5")
                except UnicodeDecodeError:
                    continue
                if 0xE000 <= ord(b5) <= 0xF8FF and 0x4E00 <= ord(g) <= 0x9FFF:
                    pua_winner = (raw, g)
                    break
            if pua_winner is not None:
                break
        if pua_winner is not None:
            pua_file = tmp_path / "pua_vs_gb.txt"
            pua_file.write_bytes(pua_winner[0] * 3)
            assert read_text_safe(pua_file) == pua_winner[1] * 3

    def test_extract_contextual_passwords_priority_dedup_multisuffix_limits_and_filters(self, tmp_path):
        parent_dir = tmp_path / "资源合集_password=pwd_parent"
        parent_dir.mkdir()

        # 同级说明文件：解压密码.txt（高优先）+ note.nfo + 超大文本 (>= 512KB 应跳过)
        (parent_dir / "解压密码.txt").write_text(
            "解压密码：pwd_file\n"           # 与文件名重复，应去重且保持高优先级
            "解压码: pwd_txt_1\n"
            "官网地址 密码: https://example.com/secret\n"  # 网址应过滤
            "镜像 密码: www.demo.org/path\n"               # www 路径应过滤
            "提取码: code_extract\n"
            "解压密码:\n"
            "pwd_next_line\n",                              # 空冒号紧邻下一行密码
            encoding="utf-8",
        )
        (parent_dir / "info.nfo").write_text("pwd=pwd_nfo_2\n", encoding="utf-8")

        # >= 512KB 大文本必须跳过
        huge_txt = parent_dir / "huge_password.txt"
        huge_txt.write_text("解压密码：huge_ignored\n" + ("A" * (512 * 1024)), encoding="utf-8")

        # 目标压缩包：含多重后缀 .part1.rar + 文件名显式密码 + 有效括号候选 + 应过滤标签
        archive_part = parent_dir / "动漫_解压密码：pwd_file [my_bracket_key]_[1080p]_【合集】_[v1.2].part1.rar"
        archive_part.write_bytes(b"rar")

        pwds = extract_contextual_passwords(archive_part)
        # 四级优先级顺序：文件名显式 > 父目录显式 > 同级 txt/nfo > 文件名括号
        assert pwds.index("pwd_file") == 0
        assert pwds.index("pwd_parent") == 1
        assert pwds.index("pwd_txt_1") < pwds.index("pwd_nfo_2") < pwds.index("my_bracket_key")
        assert "pwd_next_line" in pwds
        assert "code_extract" in pwds
        # 去重验证
        assert pwds.count("pwd_file") == 1
        # 大文本与标签/网址过滤验证
        assert "huge_ignored" not in pwds
        assert "1080p" not in pwds
        assert "合集" not in pwds
        assert "v1.2" not in pwds
        assert not any("example.com" in p or "demo.org" in p for p in pwds)

        # 多重后缀 .7z.001 剥离测试
        split_7z = tmp_path / "split_dir" / "pack_pwd=secret7z.7z.001"
        split_7z.parent.mkdir()
        split_7z.write_bytes(b"7z")
        assert extract_contextual_passwords(split_7z) == ["secret7z"]

        # 目录不存在时安全返回 []
        missing_archive = tmp_path / "never_existed_dir" / "normal_pack.zip"
        assert extract_contextual_passwords(missing_archive) == []

    def test_password_manager_record_success_new_and_existing(self, tmp_path, monkeypatch):
        pm = _make_isolated_pwd_manager(
            tmp_path,
            monkeypatch,
            initial_items=[
                {"password": "old_pwd", "tag": "手动添加", "count": 2, "last_used": 100.0},
            ],
        )

        # 1) 新密码自动以 "自动嗅探" 入库持久化
        pm.record_success("新嗅探密码_2026")
        all_pwds = pm.get_all()
        rec_new = next(r for r in all_pwds if r["password"] == "新嗅探密码_2026")
        assert rec_new["tag"] == "自动嗅探"
        assert rec_new["count"] == 1
        assert rec_new["last_used"] > 0

        # 验证磁盘持久化 + UTF-8 中文
        disk_raw = pm._file.read_text(encoding="utf-8")
        assert "新嗅探密码_2026" in disk_raw
        assert "自动嗅探" in disk_raw

        # 2) 已有密码自增频次并保持排位在前，不覆盖原标签
        pm.record_success("old_pwd")
        all_after = pm.get_all()
        assert all_after[0]["password"] == "old_pwd"
        assert all_after[0]["count"] == 3
        assert all_after[0]["tag"] == "手动添加"

    def test_pipeline_outer_and_nested_contextual_passwords_prioritized_and_recorded(self, tmp_path, monkeypatch):
        sandbox_root = tmp_path / "sb_root"
        monkeypatch.setattr(sandbox_mod, "SANDBOX_ROOT", sandbox_root)
        cfg = _make_isolated_config(tmp_path, monkeypatch, {"output_mode": "source_directory"})
        pm = _make_isolated_pwd_manager(
            tmp_path,
            monkeypatch,
            initial_items=[
                {"password": "lib_wrong_1", "tag": "常用", "count": 10, "last_used": 100.0},
                {"password": "lib_wrong_2", "tag": "常用", "count": 5, "last_used": 50.0},
            ],
        )

        # 构造外层包 + 同级 解压密码.txt，内含嵌套包 inner.zip + 说明.txt
        work_dir = tmp_path / "pkg_dir"
        work_dir.mkdir()
        inner_zip = tmp_path / "staging_inner.zip"
        _create_zip_file(inner_zip, {"final_story.txt": "nested contextual success"})

        outer_zip = work_dir / "mystery_outer.zip"
        _create_zip_file(
            outer_zip,
            {
                "inner.zip": inner_zip.read_bytes(),
                "说明.txt": "本层解压密码：inner_sniff_888\n",
            },
        )
        (work_dir / "解压密码.txt").write_text("解压密码：outer_sniff_666\n", encoding="utf-8")

        dummy_exe = tmp_path / "bz.exe"
        dummy_exe.write_bytes(b"exe")
        engine = _FakeZipEngine(
            dummy_exe,
            password_map={
                "mystery_outer.zip": "outer_sniff_666",
                "inner.zip": "inner_sniff_888",
            },
        )

        record_success_calls: list[tuple[str, str]] = []
        real_record_success = pm.record_success

        def _spy_record_success(password: str, tag: str = ""):
            record_success_calls.append((password, tag))
            return real_record_success(password, tag=tag)

        monkeypatch.setattr(pm, "record_success", _spy_record_success)

        pipeline = ExtractionPipeline(
            engine=engine,
            engine_name="Fake",
            sandbox=SandboxManager(),
            password_manager=pm,
            config=cfg,
        )
        ok = pipeline.process("t_ctx_e2e", str(outer_zip))
        assert ok is True

        # 验证外层与嵌套层均将嗅探密码置顶（从未尝试过 lib_wrong_1 / lib_wrong_2）
        tried_pwds = [pwd for _, pwd in engine.extract_calls if pwd is not None]
        assert tried_pwds == ["outer_sniff_666", "inner_sniff_888"]

        # 验证自动调用 record_success(..., tag="自动嗅探") 并入库
        assert ("outer_sniff_666", "自动嗅探") in record_success_calls
        assert ("inner_sniff_888", "自动嗅探") in record_success_calls
        saved_map = {r["password"]: r for r in pm.get_all()}
        assert saved_map["outer_sniff_666"]["tag"] == "自动嗅探"
        assert saved_map["inner_sniff_888"]["tag"] == "自动嗅探"
        assert (work_dir / "mystery_outer" / "final_story.txt").read_text(encoding="utf-8") == "nested contextual success"


# ==================== 4. 大包多密快速预检（Engine + Pipeline）====================


class TestFastPrecheckEngineAndPipeline:
    """覆盖 BandizipEngine / WinRAREngine 快速验密以及 ExtractionPipeline 大包多密预检编排。"""

    def test_bandizip_test_password_header_encrypted_probe_cache(self, tmp_path):
        bz_exe = tmp_path / "bz.exe"
        bz_exe.write_bytes(b"bz")
        arch = tmp_path / "header_enc.rar"
        arch.write_bytes(b"Rar!\x1a\x07\x01\x00dummy")

        engine = BandizipEngine(bz_exe)
        cmds: list[list[str]] = []

        def _fake_run(cmd, timeout=60, task_id=None, **kwargs):
            cmds.append(list(cmd))
            if cmd[1] == "l" and "-p:" in cmd:
                return subprocess.CompletedProcess(cmd, 2, "", "Wrong password")
            if cmd[1] == "l" and "-p:bad1" in cmd:
                return subprocess.CompletedProcess(cmd, 2, "", "Error 0xA0000003: Can't open archive")
            if cmd[1] == "l" and "-p:good_pwd" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "file1.txt\n", "")
            raise AssertionError(f"Unexpected cmd: {cmd}")

        with patch.object(engine, "_run_command", side_effect=_fake_run):
            assert engine.test_password(arch, "bad1", task_id="t1") is False
            assert engine.test_password(arch, "good_pwd", task_id="t1") is True

        # 第 1 次触发 probe (`l -p:`) + `l -p:bad1`；第 2 次命中探针缓存直接 `l -p:good_pwd`
        assert len(cmds) == 3
        assert cmds[0][1:3] == ["l", "-p:"]
        assert cmds[1][1:3] == ["l", "-p:bad1"]
        assert cmds[2][1:3] == ["l", "-p:good_pwd"]

    def test_bandizip_test_password_smallest_member_fallback_corrupt_and_abort(self, tmp_path):
        bz_exe = tmp_path / "bz.exe"
        bz_exe.write_bytes(b"bz")
        arch = tmp_path / "normal_enc.zip"
        arch.write_bytes(b"PK\x03\x04dummy")

        engine = BandizipEngine(bz_exe)
        list_stdout = (
            "2026-01-01 10:00:00 D....        0        0 folder/\n"
            "2026-01-01 10:00:00 ....A        0        0 empty.txt\n"
            "2026-01-01 10:00:00 ....A  5000000  2000000 movie.bin\n"
            "2026-01-01 10:00:00 ....A      128       64 sub/tiny.txt\n"
        )
        cmds: list[list[str]] = []

        def _fake_run(cmd, timeout=60, task_id=None, **kwargs):
            cmds.append(list(cmd))
            if cmd[1] == "l":
                return subprocess.CompletedProcess(cmd, 0, list_stdout, "")
            # 定向成员测试：错密返回 False
            if cmd[1] == "t" and "-p:wrong" in cmd and cmd[-1] == "sub/tiny.txt":
                return subprocess.CompletedProcess(cmd, 2, "", "Wrong password")
            # 定向成员遇到非密码错误 -> 触发整包 t 平滑降级
            if cmd[1] == "t" and "-p:ok_fallback" in cmd and cmd[-1] == "sub/tiny.txt":
                return subprocess.CompletedProcess(cmd, 2, "", "Cannot find specified path")
            if cmd[1] == "t" and "-p:ok_fallback" in cmd and cmd[-1] == str(arch.resolve()):
                return subprocess.CompletedProcess(cmd, 0, "Everything is Ok", "")
            # 坏包：定向与整包均返回非密码错误 -> 抛出 ExtractionFailedError
            if cmd[1] == "t" and "-p:corrupt_case" in cmd:
                return subprocess.CompletedProcess(cmd, 2, "", "CRC error: archive corrupted")
            raise AssertionError(f"Unexpected cmd: {cmd}")

        with patch.object(engine, "_run_command", side_effect=_fake_run):
            assert engine.test_password(arch, "wrong", task_id="t1") is False
            assert engine.test_password(arch, "ok_fallback", task_id="t1") is True
            with pytest.raises(ExtractionFailedError):
                engine.test_password(arch, "corrupt_case", task_id="t1")

        # 验证解析出的最小非空常规成员为 sub/tiny.txt，且触发了整包降级
        assert any(c[1] == "t" and c[-1] == "sub/tiny.txt" for c in cmds)
        assert any(c[1] == "t" and "-p:ok_fallback" in c and c[-1] == str(arch.resolve()) for c in cmds)

        # 中止状态抛出 ExtractionAbortedError
        engine.terminate_process("t_aborted")
        with pytest.raises(ExtractionAbortedError):
            engine.test_password(arch, "any", task_id="t_aborted")

    def test_winrar_test_password_header_encrypted_targeted_fallback_corrupt_and_abort(self, tmp_path):
        wr_dir = tmp_path / "WinRAR"
        wr_dir.mkdir()
        wr_exe = wr_dir / "WinRAR.exe"
        unrar_exe = wr_dir / "UnRAR.exe"
        wr_exe.write_bytes(b"wr")
        unrar_exe.write_bytes(b"unrar")

        engine = WinRAREngine(wr_exe)

        # --- 场景 A：加密文件头（probe lt 返回 rc=11 -> 缓存 -> 后续走 lb）---
        hp_arch = tmp_path / "hp_encrypted.rar"
        hp_arch.write_bytes(b"Rar!\x1a\x07\x01\x00hp")
        hp_cmds: list[list[str]] = []

        def _fake_hp_run(cmd, timeout=60, task_id=None, **kwargs):
            hp_cmds.append(list(cmd))
            if cmd[1] == "lt":
                return subprocess.CompletedProcess(cmd, 11, "", "Enter password")
            if cmd[1] == "lb" and "-pbad" in cmd:
                return subprocess.CompletedProcess(cmd, 11, "", "Incorrect password")
            if cmd[1] == "lb" and "-pgood" in cmd:
                return subprocess.CompletedProcess(cmd, 0, "secret.txt\n", "")
            raise AssertionError(f"Unexpected cmd: {cmd}")

        with patch.object(engine, "_run_command", side_effect=_fake_hp_run):
            assert engine.test_password(hp_arch, "bad", task_id="t_hp") is False
            assert engine.test_password(hp_arch, "good", task_id="t_hp") is True

        assert len(hp_cmds) == 3
        assert hp_cmds[0][1] == "lt"
        assert hp_cmds[1][1:3] == ["lb", "-pbad"]
        assert hp_cmds[2][1:3] == ["lb", "-pgood"]

        # --- 场景 B：普通加密包解析最小常规成员 -> 定向 t -> 非密码错误平滑降级整包 t + 坏包异常 ---
        norm_arch = tmp_path / "normal_enc.rar"
        norm_arch.write_bytes(b"Rar!\x1a\x07\x01\x00norm")
        lt_stdout = (
            "Name: folder_a\nType: Directory\nSize: 0\n\n"
            "Name: zero.dat\nType: File\nSize: 0\n\n"
            "Name: big_video.mp4\nType: File\nSize: 99999999\n\n"
            "Name: docs\\small.nfo\nType: File\nSize: 256\n\n"
        )
        norm_cmds: list[list[str]] = []

        def _fake_norm_run(cmd, timeout=60, task_id=None, **kwargs):
            norm_cmds.append(list(cmd))
            if cmd[1] == "lt":
                return subprocess.CompletedProcess(cmd, 0, lt_stdout, "")
            if cmd[1] == "t" and "-pwrong" in cmd and cmd[-1] == "docs\\small.nfo":
                return subprocess.CompletedProcess(cmd, 11, "", "Wrong password")
            if cmd[1] == "t" and "-pfallback_ok" in cmd and cmd[-1] == "docs\\small.nfo":
                return subprocess.CompletedProcess(cmd, 10, "", "No files to extract")
            if cmd[1] == "t" and "-pfallback_ok" in cmd and cmd[-1] == str(norm_arch.resolve()):
                return subprocess.CompletedProcess(cmd, 0, "All OK", "")
            if cmd[1] == "t" and "-pbroken" in cmd:
                return subprocess.CompletedProcess(cmd, 3, "", "CRC failed")
            raise AssertionError(f"Unexpected cmd: {cmd}")

        with patch.object(engine, "_run_command", side_effect=_fake_norm_run):
            assert engine.test_password(norm_arch, "wrong", task_id="t_norm") is False
            assert engine.test_password(norm_arch, "fallback_ok", task_id="t_norm") is True
            with pytest.raises(ExtractionFailedError):
                engine.test_password(norm_arch, "broken", task_id="t_norm")

        assert any(c[1] == "t" and c[-1] == "docs\\small.nfo" for c in norm_cmds)
        assert any(c[1] == "t" and "-pfallback_ok" in c and c[-1] == str(norm_arch.resolve()) for c in norm_cmds)

        # --- 场景 C：中止抛 ExtractionAbortedError ---
        engine.terminate_process("t_wr_abort")
        with pytest.raises(ExtractionAbortedError):
            engine.test_password(norm_arch, "any", task_id="t_wr_abort")

    def test_pipeline_fast_precheck_hit_single_extract_and_counter_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "FAST_PRECHECK_MIN_SIZE_BYTES", 10)
        monkeypatch.setattr(sandbox_mod, "SANDBOX_ROOT", tmp_path / "sb_root")
        cfg = _make_isolated_config(tmp_path, monkeypatch, {"output_mode": "source_directory"})
        pm = _make_isolated_pwd_manager(
            tmp_path,
            monkeypatch,
            initial_items=[
                {"password": "p1", "tag": "t", "count": 5, "last_used": 5.0},
                {"password": "p2", "tag": "t", "count": 4, "last_used": 4.0},
                {"password": "p3", "tag": "t", "count": 3, "last_used": 3.0},
                {"password": "secret4", "tag": "t", "count": 2, "last_used": 2.0},
                {"password": "p5", "tag": "t", "count": 1, "last_used": 1.0},
            ],
        )

        src_dir = tmp_path / "big_pkg_dir"
        src_dir.mkdir()
        big_zip = _create_zip_file(src_dir / "big_archive.zip", {"movie.txt": "payload data > 10 bytes"})

        dummy_exe = tmp_path / "bz.exe"
        dummy_exe.write_bytes(b"exe")
        engine = _FakeZipEngine(
            dummy_exe,
            supports_fast_test=True,
            password_map={"big_archive.zip": "secret4"},
        )

        layer_events: list[tuple[int, int, str]] = []
        wait_called = False

        def _wait_pwd(task_id: str):
            nonlocal wait_called
            wait_called = True
            return None

        pipeline = ExtractionPipeline(
            engine=engine,
            engine_name="FakeFast",
            sandbox=SandboxManager(),
            password_manager=pm,
            config=cfg,
            wait_for_password=_wait_pwd,
            on_layer_progress=lambda tid, pct, total, text: layer_events.append((pct, total, text)),
        )

        ok = pipeline.process("t_fast_hit", str(big_zip))
        assert ok is True
        assert wait_called is False

        # test_password 依次试探到 secret4 即命中停止（未试探 p5）
        assert [pwd for _, pwd in engine.test_calls] == ["p1", "p2", "p3", "secret4"]

        # 带密码的 extract 仅执行 1 次（仅对预检命中的 secret4 执行一次性真实解压）
        pwd_extracts = [pwd for _, pwd in engine.extract_calls if pwd is not None]
        assert pwd_extracts == ["secret4"]

        # 双计数器 total 仅为 1（预检次数绝不计入有效解压计数）
        max_total = max(total for _, total, _ in layer_events)
        assert max_total == 1
        assert (src_dir / "big_archive" / "movie.txt").read_text(encoding="utf-8") == "payload data > 10 bytes"

    def test_pipeline_fast_precheck_all_miss_zero_redundant_extracts_enters_wait(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "FAST_PRECHECK_MIN_SIZE_BYTES", 10)
        monkeypatch.setattr(sandbox_mod, "SANDBOX_ROOT", tmp_path / "sb_root")
        cfg = _make_isolated_config(tmp_path, monkeypatch, {"output_mode": "source_directory"})
        pm = _make_isolated_pwd_manager(
            tmp_path,
            monkeypatch,
            initial_items=[
                {"password": "w1", "tag": "t", "count": 4, "last_used": 4.0},
                {"password": "w2", "tag": "t", "count": 3, "last_used": 3.0},
                {"password": "w3", "tag": "t", "count": 2, "last_used": 2.0},
                {"password": "w4", "tag": "t", "count": 1, "last_used": 1.0},
            ],
        )

        src_dir = tmp_path / "miss_dir"
        src_dir.mkdir()
        big_zip = _create_zip_file(src_dir / "locked_big.zip", {"secret.txt": "manual unlock content"})

        dummy_exe = tmp_path / "bz.exe"
        dummy_exe.write_bytes(b"exe")
        engine = _FakeZipEngine(
            dummy_exe,
            supports_fast_test=True,
            password_map={"locked_big.zip": "user_manual_pwd"},
        )

        wait_calls: list[str] = []

        def _wait_pwd(task_id: str):
            wait_calls.append(task_id)
            return "user_manual_pwd"

        pipeline = ExtractionPipeline(
            engine=engine,
            engine_name="FakeFast",
            sandbox=SandboxManager(),
            password_manager=pm,
            config=cfg,
            wait_for_password=_wait_pwd,
        )

        ok = pipeline.process("t_fast_miss", str(big_zip))
        assert ok is True
        assert wait_calls == ["t_fast_miss"]
        assert [pwd for _, pwd in engine.test_calls] == ["w1", "w2", "w3", "w4"]
        # 候选密码 w1..w4 的冗余完整解压次数严格为 0，仅在 wait_for_password 后用手动密码解压 1 次
        pwd_extracts = [pwd for _, pwd in engine.extract_calls if pwd is not None]
        assert pwd_extracts == ["user_manual_pwd"]

    def test_pipeline_fast_precheck_corrupt_archive_raises_immediately(self, tmp_path, monkeypatch):
        monkeypatch.setattr(pipeline_mod, "FAST_PRECHECK_MIN_SIZE_BYTES", 10)
        cfg = _make_isolated_config(tmp_path, monkeypatch)
        pm = _make_isolated_pwd_manager(tmp_path, monkeypatch)

        arch = tmp_path / "broken_big.zip"
        arch.write_bytes(b"PK\x03\x04" + b"X" * 128)
        dest = tmp_path / "out"

        dummy_exe = tmp_path / "bz.exe"
        dummy_exe.write_bytes(b"exe")

        class _CorruptOnPrecheckEngine(_FakeZipEngine):
            def extract(self, archive, dest, password=None, timeout=300, task_id=None, on_progress=None):
                self.extract_calls.append((Path(archive).name, password))
                if password is None:
                    raise PasswordRequiredError(str(archive), "encrypted")
                raise AssertionError("extract should not be called after corrupt precheck")

            def test_password(self, archive, password=None, timeout=60, task_id=None) -> bool:
                self.test_calls.append((Path(archive).name, password))
                raise ExtractionFailedError(str(archive), 3, "CRC corrupt archive")

        engine = _CorruptOnPrecheckEngine(dummy_exe, supports_fast_test=True)
        wait_called = False

        def _wait_pwd(task_id: str):
            nonlocal wait_called
            wait_called = True
            return None

        pipeline = ExtractionPipeline(
            engine=engine,
            engine_name="FakeFast",
            password_manager=pm,
            config=cfg,
            wait_for_password=_wait_pwd,
        )

        with pytest.raises(ExtractionFailedError):
            pipeline.extract_with_smart_passwords(
                "t_corrupt", arch, str(dest), ["p1", "p2", "p3", "p4"], label="测试"
            )

        # 第一次预检遇坏包即刻上抛终止：后续密码不再试探，不调用 wait_for_password，无候选 extract
        assert [pwd for _, pwd in engine.test_calls] == ["p1"]
        assert [pwd for _, pwd in engine.extract_calls if pwd is not None] == []
        assert wait_called is False
