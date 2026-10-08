"""
自动化测试套件：Issue #1 新增三大功能全流程与边界防护验证
1. 功能一：解压成功后自动删除源文件（回收站）、同族分卷识别、非首卷过滤、dest_dir 备注纯净性
2. 功能二：密码频次排序、自学习打点 record_hit、向下兼容迁移、单例线程安全、按值删除
3. 功能三：剪贴板与 TXT 文件批量导入清洗解析、多编码自适应安全读取、批量去重入库
"""
import os
import sys
import json
import time
import shutil
import zipfile
import threading
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication(sys.argv)

from core.sandbox import (
    SandboxManager,
    is_primary_part,
    is_secondary_volume,
    get_part_group,
    send_to_recycle_bin,
    PROTECTED_ENDPOINT_EXTS,
)
from core.task_queue import TaskQueue
from data.config_manager import ConfigManager
from data.password_manager import (
    PasswordManager,
    parse_passwords_text,
    read_text_safe,
)


class TestFeature1DeleteSourceAndVolumes(unittest.TestCase):
    """功能一测试：源文件回收站删除与分卷识别/过滤。"""

    def setUp(self):
        self.scratch = PROJECT_ROOT / "tests" / "test_scratch_f1"
        if self.scratch.exists():
            shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True, exist_ok=True)

        self.cfg = ConfigManager()
        self._orig_del = self.cfg.get("delete_source_after_extract")
        self._orig_mode = self.cfg.get("output_mode")
        self._orig_custom = self.cfg.get("custom_output_path")

        self.cfg.set("output_mode", "source_directory")
        self.cfg.set("custom_output_path", "")

    def tearDown(self):
        self.cfg.set("delete_source_after_extract", self._orig_del)
        self.cfg.set("output_mode", self._orig_mode)
        self.cfg.set("custom_output_path", self._orig_custom)
        if self.scratch.exists():
            shutil.rmtree(self.scratch, ignore_errors=True)

    def test_volume_naming_detection(self):
        """测试分卷命名判定：主卷与非首卷。"""
        # 主卷判定
        self.assertTrue(is_primary_part("game.part1.rar"))
        self.assertTrue(is_primary_part("game.part01.rar"))
        self.assertTrue(is_primary_part("game.part001.rar"))
        self.assertTrue(is_primary_part("archive.7z.001"))
        self.assertTrue(is_primary_part("data.zip.001"))
        self.assertFalse(is_primary_part("normal.zip"))
        self.assertFalse(is_primary_part("normal.rar"))
        self.assertFalse(is_primary_part("game.part2.rar"))
        self.assertFalse(is_primary_part("archive.7z.002"))

        # 次卷判定
        self.assertFalse(is_secondary_volume("game.part1.rar"))
        self.assertFalse(is_secondary_volume("game.part01.rar"))
        self.assertFalse(is_secondary_volume("archive.7z.001"))
        self.assertFalse(is_secondary_volume("normal.zip"))
        self.assertTrue(is_secondary_volume("game.part2.rar"))
        self.assertTrue(is_secondary_volume("game.part02.rar"))
        self.assertTrue(is_secondary_volume("game.part10.rar"))
        self.assertTrue(is_secondary_volume("archive.7z.002"))
        self.assertTrue(is_secondary_volume("archive.7z.012"))
        self.assertTrue(is_secondary_volume("data.zip.002"))

    def test_get_part_group_detection(self):
        """测试同族分卷查找能力（支持补零与不同格式）。"""
        # 创建 RAR 补零分卷族
        rar1 = self.scratch / "movie.part01.rar"
        rar2 = self.scratch / "movie.part02.rar"
        rar3 = self.scratch / "movie.part03.rar"
        rar1.write_bytes(b"r1")
        rar2.write_bytes(b"r2")
        rar3.write_bytes(b"r3")

        group = get_part_group(rar1)
        self.assertEqual(len(group), 3)
        self.assertEqual([p.name for p in group], ["movie.part01.rar", "movie.part02.rar", "movie.part03.rar"])

        # 从次卷查询也应能级联发现同族
        group_from_part2 = get_part_group(rar2)
        self.assertEqual(len(group_from_part2), 3)

        # 7z.001 分卷族
        z1 = self.scratch / "pack.7z.001"
        z2 = self.scratch / "pack.7z.002"
        z1.write_bytes(b"z1")
        z2.write_bytes(b"z2")
        group_7z = get_part_group(z1)
        self.assertEqual(len(group_7z), 2)
        self.assertEqual([p.name for p in group_7z], ["pack.7z.001", "pack.7z.002"])

        # 单文件非分卷
        single = self.scratch / "single.zip"
        single.write_bytes(b"single")
        self.assertEqual(get_part_group(single), [single.resolve()])

    def test_send_to_recycle_bin(self):
        """测试原生 Windows 回收站静默删除。"""
        temp_file = self.scratch / "test_recycle.bin"
        temp_file.write_text("temporary content", encoding="utf-8")
        self.assertTrue(temp_file.exists())

        res = send_to_recycle_bin(temp_file)
        self.assertTrue(res)
        self.assertFalse(temp_file.exists())

        # 删除不存在的文件返回 False
        res_none = send_to_recycle_bin(self.scratch / "not_exist.txt")
        self.assertFalse(res_none)

    def test_task_queue_auto_delete_source_when_enabled(self):
        """测试解压成功后自动删除源压缩包（移至回收站）并保持 dest_dir 纯净。"""
        self.cfg.set("delete_source_after_extract", True)
        self.cfg.set("min_file_size_mb", 0)

        # 构造有效 zip
        zip_path = self.scratch / "test_success.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("hello.txt", "hello world")

        self.assertTrue(zip_path.exists())

        tq = TaskQueue()
        tq.start()

        status_records = []
        tq.task_status_changed.connect(
            lambda tid, fn, status, eng, remark: status_records.append((status, remark))
        )

        tq._process_task("t1", str(zip_path))
        tq.shutdown(wait=True)

        # 源文件应已被移入回收站（磁盘中不存在）
        self.assertFalse(zip_path.exists(), "源压缩包应在成功解压后被删除")

        # 检查交付成果
        success_status = [r for r in status_records if r[0] == "成功"]
        self.assertTrue(len(success_status) > 0, "任务应成功交付")
        dest_dir = success_status[-1][1]
        self.assertTrue(Path(dest_dir).exists(), f"交付目录必须存在: {dest_dir}")
        self.assertTrue((Path(dest_dir) / "hello.txt").exists())

        # 验证 dest_dir 绝无包含任何删除失败等污染字样
        self.assertEqual(dest_dir, str(Path(dest_dir).resolve()))

    def test_task_queue_preserve_source_when_disabled(self):
        """测试开关关闭时保留源文件。"""
        self.cfg.set("delete_source_after_extract", False)
        self.cfg.set("min_file_size_mb", 0)

        zip_path = self.scratch / "preserve_test.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("file.txt", "content")

        tq = TaskQueue()
        tq.start()
        tq._process_task("t2", str(zip_path))
        tq.shutdown(wait=True)

        self.assertTrue(zip_path.exists(), "关闭删除开关时源压缩包必须完好保留")

    def test_task_queue_preserve_source_on_failure(self):
        """测试解压失败时，即便开启删除也绝对保留源文件。"""
        self.cfg.set("delete_source_after_extract", True)
        self.cfg.set("min_file_size_mb", 0)

        broken_zip = self.scratch / "broken.zip"
        broken_zip.write_bytes(b"corrupted binary data not a zip")

        tq = TaskQueue()
        tq.start()
        tq._process_task("t3", str(broken_zip))
        tq.shutdown(wait=True)

        self.assertTrue(broken_zip.exists(), "解压失败时严禁删除源文件")

    def test_task_queue_skip_secondary_volume_in_scan(self):
        """测试递归扫描目录时自动跳过非首卷分卷并提示。"""
        self.cfg.set("min_file_size_mb", 0)
        p1 = self.scratch / "pkg.part01.rar"
        p2 = self.scratch / "pkg.part02.rar"
        p1.write_bytes(b"p1")
        p2.write_bytes(b"p2")

        tq = TaskQueue()
        skipped_events = []
        tq.task_status_changed.connect(
            lambda tid, fn, status, eng, remark: skipped_events.append((fn, status))
        )

        tq.scan_directory(str(self.scratch))
        tq.shutdown(wait=False)

        # pkg.part02.rar 必须被标记为 已跳过(非首卷分卷)
        p2_skipped = any(fn == "pkg.part02.rar" and "已跳过" in st for fn, st in skipped_events)
        self.assertTrue(p2_skipped, "次卷 pkg.part02.rar 必须在扫描时被拦截并跳过")

    def test_generic_split_volume_001_detection(self):
        """测试纯数字分卷 (.001, .002) 的主次卷判定与同族分卷查找。"""
        self.assertTrue(is_primary_part("backup.001"))
        self.assertFalse(is_primary_part("backup.002"))
        self.assertTrue(is_secondary_volume("backup.002"))
        self.assertFalse(is_secondary_volume("backup.001"))

        b1 = self.scratch / "backup.001"
        b2 = self.scratch / "backup.002"
        b3 = self.scratch / "backup.003"
        b1.write_bytes(b"b1")
        b2.write_bytes(b"b2")
        b3.write_bytes(b"b3")

        group = get_part_group(b1)
        self.assertEqual(len(group), 3)
        self.assertEqual([p.name for p in group], ["backup.001", "backup.002", "backup.003"])

    def test_classic_split_volume_detection(self):
        """测试经典分卷 (.rar/.r00, .zip/.z01) 判定与查找。"""
        self.assertTrue(is_secondary_volume("game.r00"))
        self.assertTrue(is_secondary_volume("game.r01"))
        self.assertTrue(is_secondary_volume("data.z01"))
        self.assertTrue(is_secondary_volume("data.z02"))

        r_main = self.scratch / "classic.rar"
        r_part1 = self.scratch / "classic.r00"
        r_part2 = self.scratch / "classic.r01"
        r_main.write_bytes(b"rmain")
        r_part1.write_bytes(b"r00")
        r_part2.write_bytes(b"r01")

        group = get_part_group(r_main)
        self.assertEqual(len(group), 3)
        self.assertIn(r_main.resolve(), [p.resolve() for p in group])
        self.assertIn(r_part1.resolve(), [p.resolve() for p in group])
        self.assertIn(r_part2.resolve(), [p.resolve() for p in group])

    def test_task_queue_scan_directory_with_split_7z(self):
        """测试递归扫描目录时识别并处理 .7z.001 与 .7z.002 分卷。"""
        self.cfg.set("min_file_size_mb", 0)
        z1 = self.scratch / "archive.7z.001"
        z2 = self.scratch / "archive.7z.002"
        z1.write_bytes(b"z1")
        z2.write_bytes(b"z2")

        tq = TaskQueue()
        events = []
        tq.task_status_changed.connect(
            lambda tid, fn, status, eng, remark: events.append((fn, status))
        )

        tq.scan_directory(str(self.scratch))
        tq.shutdown(wait=False)

        # archive.7z.001 应该排队，archive.7z.002 应该被跳过
        z1_queued = any(fn == "archive.7z.001" and "排队中" in st for fn, st in events)
        z2_skipped = any(fn == "archive.7z.002" and "已跳过" in st for fn, st in events)
        self.assertTrue(z1_queued, "首卷 archive.7z.001 必须被递归扫描发现并排队")
        self.assertTrue(z2_skipped, "次卷 archive.7z.002 必须被识别并标记为已跳过")


class TestFeature2PasswordFrequency(unittest.TestCase):
    """功能二测试：密码使用频率自学习、排序、单例与线程安全。"""

    def setUp(self):
        self.scratch = PROJECT_ROOT / "tests" / "test_scratch_f2"
        if self.scratch.exists():
            shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True, exist_ok=True)

        self.mgr = PasswordManager()
        self._orig_file = self.mgr._file
        self.test_json = self.scratch / "passwords.json"
        self.mgr._file = self.test_json
        self.mgr.clear_all()

    def tearDown(self):
        self.mgr._file = self._orig_file
        self.mgr.reload()
        if self.scratch.exists():
            shutil.rmtree(self.scratch, ignore_errors=True)

    def test_singleton_identity(self):
        """测试 PasswordManager 单例性。"""
        mgr1 = PasswordManager()
        mgr2 = PasswordManager()
        self.assertIs(mgr1, mgr2)

    def test_legacy_format_backward_compatibility(self):
        """测试旧格式向前兼容加载（纯字符串与无 count 的字典）。"""
        legacy_data = [
            "plain_old_pwd",
            {"password": "dict_old_pwd", "tag": "旧标签"},
            {"password": "high_count_pwd", "tag": "高频", "count": 10, "last_used": 1700000000.0},
        ]
        with open(self.test_json, "w", encoding="utf-8") as f:
            json.dump(legacy_data, f, ensure_ascii=False)

        self.mgr.reload()
        all_records = self.mgr.get_all()

        self.assertEqual(len(all_records), 3)
        # 高频应排在第一位
        self.assertEqual(all_records[0]["password"], "high_count_pwd")
        self.assertEqual(all_records[0]["count"], 10)

        # 检查纯字符串与旧字典已补充完整字段
        passwords = {r["password"]: r for r in all_records}
        self.assertEqual(passwords["plain_old_pwd"]["count"], 0)
        self.assertEqual(passwords["plain_old_pwd"]["last_used"], 0.0)
        self.assertEqual(passwords["dict_old_pwd"]["count"], 0)
        self.assertEqual(passwords["dict_old_pwd"]["tag"], "旧标签")

    def test_record_hit_and_dynamic_sorting(self):
        """测试 record_hit 触发自学习频次增加与即时排序。"""
        self.mgr.add("pwd_a", "[测试]")
        self.mgr.add("pwd_b", "[测试]")
        self.mgr.add("pwd_c", "[测试]")

        # 对 pwd_b 命中 3 次，pwd_c 命中 5 次
        for _ in range(3):
            self.mgr.record_hit("pwd_b")
        for _ in range(5):
            self.mgr.record_hit("pwd_c")

        records = self.mgr.get_all()
        self.assertEqual(records[0]["password"], "pwd_c")
        self.assertEqual(records[0]["count"], 5)
        self.assertEqual(records[1]["password"], "pwd_b")
        self.assertEqual(records[1]["count"], 3)
        self.assertEqual(records[2]["password"], "pwd_a")
        self.assertEqual(records[2]["count"], 0)

        # 验证磁盘持久化文件也正确降序
        with open(self.test_json, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        self.assertEqual(disk_data[0]["password"], "pwd_c")

    def test_delete_by_password(self):
        """测试按密码值精确删除，避免表格行序漂移导致的误删。"""
        self.mgr.add("first_to_delete", "[标签1]")
        self.mgr.add("second_to_keep", "[标签2]")

        # 频次打点让 second_to_keep 排到前面
        self.mgr.record_hit("second_to_keep")

        # 按字符串精确删除 first_to_delete
        deleted = self.mgr.delete_by_password("first_to_delete")
        self.assertTrue(deleted)
        self.assertEqual(self.mgr.count, 1)
        self.assertEqual(self.mgr.get_all()[0]["password"], "second_to_keep")

        # 删除不存在的密码返回 False
        self.assertFalse(self.mgr.delete_by_password("not_in_db"))

    def test_thread_safety_concurrency(self):
        """测试多线程高并发读写自学习与批量导入，无锁死与数据污染。"""
        threads = []
        errors = []

        def worker_hits(pwd: str, count: int):
            try:
                for _ in range(count):
                    self.mgr.record_hit(pwd)
                    _ = self.mgr.get_all()
            except Exception as e:
                errors.append(e)

        for i in range(8):
            t = threading.Thread(target=worker_hits, args=(f"thread_pwd_{i}", 20))
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"并发读写出现异常: {errors}")
        all_data = self.mgr.get_all()
        self.assertEqual(len(all_data), 8)
        # 每个线程命中 20 次
        for item in all_data:
            self.assertEqual(item["count"], 20)

    def test_empty_password_rejection(self):
        """测试添加或打点空密码时被严格拦截，杜绝脏数据。"""
        initial_count = self.mgr.count
        res1 = self.mgr.add("", "[空密码]")
        res2 = self.mgr.add("   ", "[纯空格]")
        self.assertEqual(res1, -1)
        self.assertEqual(res2, -1)
        self.assertEqual(self.mgr.count, initial_count)

        self.mgr.record_hit("")
        self.mgr.record_hit("   ")
        self.assertEqual(self.mgr.count, initial_count)

    def test_sort_cache_safe_against_none_and_corrupt_types(self):
        """测试记录存在 count=None 或非数值类型时排序不抛出 TypeError。"""
        corrupted_data = [
            {"password": "p1", "tag": "t1", "count": None, "last_used": None},
            {"password": "p2", "tag": "t2", "count": 10, "last_used": 100.0},
            {"password": "p3", "tag": "t3", "count": "invalid", "last_used": 50.0},
        ]
        with open(self.test_json, "w", encoding="utf-8") as f:
            json.dump(corrupted_data, f, ensure_ascii=False)

        self.mgr.reload()
        records = self.mgr.get_all()
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["password"], "p2")


class TestFeature3BatchImport(unittest.TestCase):
    """功能三测试：剪贴板/文件批量导入解析、文本清洗与安全解码。"""

    def setUp(self):
        self.scratch = PROJECT_ROOT / "tests" / "test_scratch_f3"
        if self.scratch.exists():
            shutil.rmtree(self.scratch, ignore_errors=True)
        self.scratch.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self.scratch.exists():
            shutil.rmtree(self.scratch, ignore_errors=True)

    def test_parse_passwords_text_cleansing(self):
        """测试复杂格式密码提取引擎（过滤注释、前缀、抽取内嵌密码、去重）。"""
        sample_text = """
        # 这是注释行，应该被忽略
        // 这一行也是注释
        ; ini 风格注释

        123456
        解压密码: secret_pass_1
        【解压码】：secret_pass_2
        密码=secret_pass_3
        pwd: secret_pass_4
        提取码: 8888
        [密码] ：中文密码123

        # 超长复杂论坛贴文本：
        资源发布：某某大型游戏完整典藏版迅雷网盘下载 链接：https://pan.baidu.com/s/abcdef 提取码：9999 密码：game_special_pwd_2026 祝大家游玩愉快！

        # 重复项
        secret_pass_1
        123456
        """

        extracted = parse_passwords_text(sample_text)
        expected_items = [
            "123456",
            "secret_pass_1",
            "secret_pass_2",
            "secret_pass_3",
            "secret_pass_4",
            "8888",
            "中文密码123",
            "game_special_pwd_2026",
        ]

        for expected in expected_items:
            self.assertIn(expected, extracted, f"提取结果中应包含 '{expected}'")

        # 确保自动去重（列表内每个元素唯一）
        self.assertEqual(len(extracted), len(set(extracted)))
        # 确保注释未被作为密码导入
        self.assertNotIn("这是注释行，应该被忽略", extracted)

    def test_parse_passwords_text_with_inline_on_short_lines_and_wrappers(self):
        """测试短行内嵌密码提取以及剥离括号与引号包装符。"""
        text = """
        论坛专享 密码: short_pwd_1
        网盘分享 密码：short_pwd_2
        解压密码: "quoted_pwd_1"
        密码: 'quoted_pwd_2'
        解压码：“quoted_cn_1”
        解压密码：【bracket_cn_1】
        密码: [bracket_en_1]
        提取码: <angle_pwd>
        """
        extracted = parse_passwords_text(text)
        expected = [
            "short_pwd_1",
            "short_pwd_2",
            "quoted_pwd_1",
            "quoted_pwd_2",
            "quoted_cn_1",
            "bracket_cn_1",
            "bracket_en_1",
            "angle_pwd",
        ]
        for exp in expected:
            self.assertIn(exp, extracted, f"应成功提取清洗后的密码: '{exp}'")

    def test_read_text_safe_multi_encoding(self):
        """测试多编码自适应读取（UTF-8, UTF-8-BOM, GB18030/GBK, Big5）。"""
        chinese_content = "解压密码: 中文密码888\n123456\n"

        # 1. UTF-8
        f_utf8 = self.scratch / "pwd_utf8.txt"
        f_utf8.write_bytes(chinese_content.encode("utf-8"))
        self.assertEqual(read_text_safe(f_utf8), chinese_content)

        # 2. UTF-8 with BOM
        f_bom = self.scratch / "pwd_bom.txt"
        f_bom.write_bytes(chinese_content.encode("utf-8-sig"))
        self.assertEqual(read_text_safe(f_bom), chinese_content)

        # 3. GB18030 / GBK
        f_gbk = self.scratch / "pwd_gbk.txt"
        f_gbk.write_bytes(chinese_content.encode("gb18030"))
        self.assertEqual(read_text_safe(f_gbk), chinese_content)

        # 4. Big5
        big5_content = "解壓密碼: 繁體密碼888\n"
        f_big5 = self.scratch / "pwd_big5.txt"
        f_big5.write_bytes(big5_content.encode("big5"))
        self.assertEqual(read_text_safe(f_big5), big5_content)

    def test_batch_add_deduplication(self):
        """测试 PasswordManager.batch_add 的入库统计与去重机制。"""
        mgr = PasswordManager()
        test_json = self.scratch / "batch_passwords.json"
        orig_file = mgr._file
        mgr._file = test_json
        mgr.clear_all()

        try:
            # 首次添加 3 个密码
            added, dups = mgr.batch_add(["pwd1", "pwd2", "pwd3"], tag="[首次导入]")
            self.assertEqual(added, 3)
            self.assertEqual(dups, 0)
            self.assertEqual(mgr.count, 3)

            # 再次批量添加（包含重复项与新项）
            added2, dups2 = mgr.batch_add(["pwd2", "pwd3", "pwd4", "pwd5"], tag="[二次导入]")
            self.assertEqual(added2, 2)
            self.assertEqual(dups2, 2)
            self.assertEqual(mgr.count, 5)

            # 验证新项被赋予初始值
            all_records = mgr.get_all()
            p4_record = next(r for r in all_records if r["password"] == "pwd4")
            self.assertEqual(p4_record["count"], 0)
            self.assertEqual(p4_record["tag"], "[二次导入]")

        finally:
            mgr._file = orig_file
            mgr.reload()


class TestFeatureUIComponents(unittest.TestCase):
    """UI 组件交互测试：设置面板、主窗口快捷开关、密码管理面板。"""

    def setUp(self):
        from ui.settings_dialog import SettingsDialog
        from ui.password_dialog import PasswordDialog
        from ui.main_window import MainWindow

        self.SettingsDialog = SettingsDialog
        self.PasswordDialog = PasswordDialog
        self.MainWindow = MainWindow

        self.cfg = ConfigManager()
        self._orig_del = self.cfg.get("delete_source_after_extract")

    def tearDown(self):
        self.cfg.set("delete_source_after_extract", self._orig_del)
        self.cfg.save()

    def test_settings_dialog_delete_source_checkbox(self):
        """测试设置对话框中的自动删除复选框读写与保存。"""
        self.cfg.set("delete_source_after_extract", False)
        dialog = self.SettingsDialog()
        self.assertFalse(dialog._cb_delete_source.isChecked())

        dialog._cb_delete_source.setChecked(True)
        dialog._on_save()
        self.assertTrue(self.cfg.get("delete_source_after_extract"))

        # 再次打开应保持勾选
        dialog2 = self.SettingsDialog()
        self.assertTrue(dialog2._cb_delete_source.isChecked())

    def test_main_window_quick_toggle_sync(self):
        """测试主窗口状态栏快捷勾选框与 ConfigManager 实时联动。"""
        self.cfg.set("delete_source_after_extract", False)
        win = self.MainWindow()
        try:
            self.assertFalse(win._cb_delete_source.isChecked())

            # 用户在主窗口界面勾选
            win._cb_delete_source.setChecked(True)
            self.assertTrue(self.cfg.get("delete_source_after_extract"))

            # 用户取消勾选
            win._cb_delete_source.setChecked(False)
            self.assertFalse(self.cfg.get("delete_source_after_extract"))
        finally:
            win.close()

    def test_password_dialog_structure_and_safe_delete(self):
        """测试密码管理窗口 3 列结构、两行动作栏与安全删除。"""
        mgr = PasswordManager()
        mgr.clear_all()
        mgr.add("pwd_to_remove", "[测试标签]")

        dlg = self.PasswordDialog()
        try:
            # 验证表格 3 列: 密码、使用次数、标签
            self.assertEqual(dlg._table.columnCount(), 3)
            self.assertEqual(dlg._table.horizontalHeaderItem(0).text(), "密码（明文）")
            self.assertEqual(dlg._table.horizontalHeaderItem(1).text(), "使用次数")
            self.assertEqual(dlg._table.horizontalHeaderItem(2).text(), "标签")

            # 验证两行按钮均已初始化
            self.assertIsNotNone(dlg._btn_add)
            self.assertIsNotNone(dlg._btn_import_clip)
            self.assertIsNotNone(dlg._btn_import_file)
            self.assertIsNotNone(dlg._btn_delete)
            self.assertIsNotNone(dlg._btn_clear)

            # 验证按密码值删除
            self.assertEqual(dlg._table.rowCount(), 1)
            item = dlg._table.item(0, 0)
            self.assertEqual(item.text(), "pwd_to_remove")

            # 调用底层精确删除
            mgr.delete_by_password("pwd_to_remove")
            dlg._refresh_table()
            self.assertEqual(dlg._table.rowCount(), 0)
        finally:
            dlg.close()

    def test_main_window_table_receives_skipped_secondary_volume(self):
        """测试主窗口收到非首卷或过小跳过状态时，表格成功创建行而非丢失记录。"""
        win = self.MainWindow()
        try:
            initial_rows = win._table.rowCount()
            # 模拟递归扫描发射的非首卷跳过信号
            win._on_task_status_changed(
                "task_skip_1", "game.part02.rar", "已跳过(非首卷分卷)", "WinRAR", "由首卷统一解压"
            )
            self.assertEqual(win._table.rowCount(), initial_rows + 1)
            row = win._task_rows.get("task_skip_1")
            self.assertIsNotNone(row)
            self.assertEqual(win._table.item(row, 0).text(), "game.part02.rar")
            self.assertEqual(win._table.item(row, 1).text(), "已跳过(非首卷分卷)")
            self.assertEqual(win._table.item(row, 3).text(), "由首卷统一解压")

            # 模拟文件过小跳过信号
            win._on_task_status_changed(
                "task_skip_2", "tiny.zip", "已跳过(文件过小)", "WinRAR", "文件过小跳过"
            )
            self.assertEqual(win._table.rowCount(), initial_rows + 2)
            row2 = win._task_rows.get("task_skip_2")
            self.assertIsNotNone(row2)
            self.assertEqual(win._table.item(row2, 0).text(), "tiny.zip")
            self.assertEqual(win._table.item(row2, 1).text(), "已跳过(文件过小)")
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
