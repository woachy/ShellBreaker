"""
自动化验证测试：嵌套压缩包解压层级结构验证
测试目标：彻底消除 _tmp_* 中间包装，实现外包文件夹 (第1层) -> 实际业务内容 (第2层) 的绝对两层结构。

用例覆盖：
- Case 1: 单层压缩包 (直接解压，验证依然是 2 层)
- Case 2: 2层嵌套伪装包 (outer.mp4 伪装包内含 inner.zip，解压后验证 2 层且无任何 _tmp_* 目录)
- Case 3: 4层多重嵌套包 (A.zip -> B.zip -> C.zip -> D.zip -> content.txt，解压后验证 content.txt 直接位于外包下)
- Case 4: 嵌套内含文件夹 (inner.zip 内含 folder/file.txt，验证 folder 及其内容被完整保留且移至外包下)
- Case 5: 重名防覆盖机制 (相同文件名在嵌套解压上移时自动追加 _1, _2 等后缀且保留扩展名)
- Case 6: 沙箱残留 _tmp_ 解套与清理安全网 (unwrap_temp_dirs 验证)
- Case 7: 原生游戏本体文件夹绝对熔断与深层备份 zip 保护验证 (yhsj 场景防穿透)
- Case 8: 状态流转验证 (无密码压缩包全程绝不发出“验证密码”状态)
"""
import os
import sys
import shutil
import zipfile
import unittest
from pathlib import Path

# 添加项目根目录到 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QCoreApplication

# 初始化 QCoreApplication 以支持 Qt 信号
app = QCoreApplication.instance() or QCoreApplication([])

from core.task_queue import TaskQueue
from core.sandbox import SandboxManager, ensure_unique_path
from data.config_manager import ConfigManager


class TestNestingLayers(unittest.TestCase):
    """验证解压精简为两层结构及消除 _tmp_* 包装的自动化测试。"""

    def setUp(self):
        self.test_dir = PROJECT_ROOT / "tests" / "test_scratch"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)
        self.test_dir.mkdir(parents=True, exist_ok=True)

        self.cfg = ConfigManager()
        self._orig_output_mode = self.cfg.get("output_mode")
        self._orig_custom_path = self.cfg.get("custom_output_path")
        self.cfg.set("output_mode", "source_directory")
        self.cfg.set("custom_output_path", str(self.test_dir))

        self.queue = TaskQueue()
        self.queue._ensure_engine()
        if self.queue._engine is None:
            self.skipTest("测试需要可用的解压引擎 (Bandizip 或 WinRAR)")

    def tearDown(self):
        self.cfg.set("output_mode", self._orig_output_mode)
        self.cfg.set("custom_output_path", self._orig_custom_path)
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_case_1_single_layer_zip(self):
        """Case 1: 单层压缩包 (直接解压，验证依然是 2 层)"""
        archive_path = self.test_dir / "case1_outer.zip"
        content_filename = "case1.txt"
        expected_content = "Hello Case 1 Single Layer"

        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(content_filename, expected_content)

        # 执行任务
        self.queue._process_task("task_c1", str(archive_path))

        # 验证交付路径 (第 1 层：外包文件夹)
        dest_dir = self.test_dir / "case1_outer"
        self.assertTrue(dest_dir.exists(), f"交付目录不存在: {dest_dir}")
        self.assertTrue(dest_dir.is_dir(), f"交付目录不是文件夹: {dest_dir}")

        # 验证业务内容 (第 2 层：实际解压内容)
        extracted_file = dest_dir / content_filename
        self.assertTrue(extracted_file.exists(), f"解压内容缺失: {extracted_file}")
        self.assertEqual(extracted_file.read_text(encoding="utf-8"), expected_content)

        # 验证绝无任何 _tmp_* 目录
        all_items = [p.name for p in dest_dir.iterdir()]
        self.assertFalse(any(name.startswith("_tmp_") for name in all_items), f"存在 _tmp_ 残留: {all_items}")
        # 验证总共只有 2 层结构：dest_dir (1) -> case1.txt (2)
        self.assertEqual(len(all_items), 1)

    def test_case_2_two_layer_disguised_nested(self):
        """Case 2: 2层嵌套伪装包 (outer.mp4 伪装包内含 inner.zip，解压后验证 2 层且无任何 _tmp_* 目录)"""
        inner_zip = self.test_dir / "inner.zip"
        content_filename = "case2.txt"
        expected_content = "Hello Case 2 Disguised Nested"

        with zipfile.ZipFile(inner_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(content_filename, expected_content)

        # 外层为伪装包 outer.mp4
        outer_mp4 = self.test_dir / "case2_outer.mp4"
        with zipfile.ZipFile(outer_mp4, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(inner_zip, arcname="inner.zip")

        # 删除中间生成的临时 inner.zip
        inner_zip.unlink()

        # 执行任务
        self.queue._process_task("task_c2", str(outer_mp4))

        # 验证交付路径 (第 1 层：外包文件夹 case2_outer)
        dest_dir = self.test_dir / "case2_outer"
        self.assertTrue(dest_dir.exists(), f"交付目录不存在: {dest_dir}")

        # 验证业务内容 (第 2 层：case2.txt 直接位于 case2_outer 根下)
        extracted_file = dest_dir / content_filename
        self.assertTrue(extracted_file.exists(), f"解压内容缺失: {extracted_file}")
        self.assertEqual(extracted_file.read_text(encoding="utf-8"), expected_content)

        # 验证中间压缩包已删除，且绝对无任何 _tmp_* 目录
        all_items = [p.name for p in dest_dir.iterdir()]
        self.assertNotIn("inner.zip", all_items, "中间压缩包 inner.zip 未被删除")
        self.assertFalse(any(name.startswith("_tmp_") for name in all_items), f"存在 _tmp_ 残留: {all_items}")
        self.assertEqual(len(all_items), 1)

    def test_case_3_four_layer_deeply_nested(self):
        """Case 3: 4层多重嵌套包 (A.zip -> B.zip -> C.zip -> D.zip -> content.txt)"""
        d_zip = self.test_dir / "D.zip"
        content_filename = "deep_content.txt"
        expected_content = "Deep Level 4 Content"

        with zipfile.ZipFile(d_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(content_filename, expected_content)

        c_zip = self.test_dir / "C.zip"
        with zipfile.ZipFile(c_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(d_zip, arcname="D.zip")
        d_zip.unlink()

        b_zip = self.test_dir / "B.zip"
        with zipfile.ZipFile(b_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(c_zip, arcname="C.zip")
        c_zip.unlink()

        a_zip = self.test_dir / "case3_A.zip"
        with zipfile.ZipFile(a_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(b_zip, arcname="B.zip")
        b_zip.unlink()

        # 执行任务
        self.queue._process_task("task_c3", str(a_zip))

        # 验证交付路径 (第 1 层：case3_A)
        dest_dir = self.test_dir / "case3_A"
        self.assertTrue(dest_dir.exists(), f"交付目录不存在: {dest_dir}")

        # 验证业务内容 (第 2 层：deep_content.txt 直接位于 case3_A 根下)
        extracted_file = dest_dir / content_filename
        self.assertTrue(extracted_file.exists(), f"深层解压内容缺失: {extracted_file}")
        self.assertEqual(extracted_file.read_text(encoding="utf-8"), expected_content)

        # 验证所有中间压缩包 (B, C, D) 彻底消失，无任何 _tmp_* 壳
        all_items = [p.name for p in dest_dir.iterdir()]
        self.assertNotIn("B.zip", all_items)
        self.assertNotIn("C.zip", all_items)
        self.assertNotIn("D.zip", all_items)
        self.assertFalse(any(name.startswith("_tmp_") for name in all_items), f"存在 _tmp_ 残留: {all_items}")
        self.assertEqual(len(all_items), 1)

    def test_case_4_nested_with_folders(self):
        """Case 4: 嵌套内含文件夹 (inner.zip 内含 folder/file.txt，验证 folder 及其内容被完整保留且移至外包下)"""
        inner_zip = self.test_dir / "inner.zip"
        folder_file = "my_folder/file.txt"
        sub_file = "my_folder/sub_folder/sub.txt"
        content_1 = "Folder File 1"
        content_2 = "Sub Folder File 2"

        with zipfile.ZipFile(inner_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(folder_file, content_1)
            zf.writestr(sub_file, content_2)

        outer_zip = self.test_dir / "case4_outer.zip"
        with zipfile.ZipFile(outer_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(inner_zip, arcname="inner.zip")
        inner_zip.unlink()

        # 执行任务
        self.queue._process_task("task_c4", str(outer_zip))

        # 验证交付路径 (第 1 层：case4_outer)
        dest_dir = self.test_dir / "case4_outer"
        self.assertTrue(dest_dir.exists(), f"交付目录不存在: {dest_dir}")

        # 验证业务文件夹 (第 2 层：my_folder) 完整保留且直接位于外包文件夹下
        folder_dir = dest_dir / "my_folder"
        self.assertTrue(folder_dir.exists(), "my_folder 不存在")
        self.assertTrue(folder_dir.is_dir(), "my_folder 不是文件夹")

        # 验证业务文件夹内部结构完整性
        f1 = folder_dir / "file.txt"
        self.assertTrue(f1.exists())
        self.assertEqual(f1.read_text(encoding="utf-8"), content_1)

        f2 = folder_dir / "sub_folder" / "sub.txt"
        self.assertTrue(f2.exists())
        self.assertEqual(f2.read_text(encoding="utf-8"), content_2)

        # 验证无任何 _tmp_* 目录
        all_items = [p.name for p in dest_dir.iterdir()]
        self.assertNotIn("inner.zip", all_items)
        self.assertFalse(any(name.startswith("_tmp_") for name in all_items), f"存在 _tmp_ 残留: {all_items}")
        self.assertEqual(len(all_items), 1)

    def test_case_5_name_collision_avoidance(self):
        """Case 5: 上移重名防覆盖 (同名文件自动追加 _1, _2 等且保留扩展名)"""
        p1 = self.test_dir / "sample.txt"
        p1.write_text("v1", encoding="utf-8")

        p2 = ensure_unique_path(p1)
        self.assertEqual(p2.name, "sample_1.txt")
        p2.write_text("v2", encoding="utf-8")

        p3 = ensure_unique_path(p1)
        self.assertEqual(p3.name, "sample_2.txt")

        # 验证目录重名
        d1 = self.test_dir / "test_dir"
        d1.mkdir()
        d2 = ensure_unique_path(d1)
        self.assertEqual(d2.name, "test_dir_1")

    def test_case_6_unwrap_temp_dirs_safety_net(self):
        """Case 6: 沙箱残留 _tmp_ 解套与清理安全网验证"""
        sandbox_out = self.test_dir / "mock_sandbox" / "output"
        sandbox_out.mkdir(parents=True, exist_ok=True)

        # 创建一个空 _tmp_ 目录
        empty_tmp = sandbox_out / "_tmp_empty"
        empty_tmp.mkdir()

        # 创建一个含有内容的 _tmp_ 目录
        nested_tmp = sandbox_out / "_tmp_data"
        nested_tmp.mkdir()
        mock_file = nested_tmp / "payload.txt"
        mock_file.write_text("safe content", encoding="utf-8")

        # 调用安全网清理解套
        SandboxManager.unwrap_temp_dirs(sandbox_out)

        # 验证 _tmp_ 目录均已被删除
        self.assertFalse(empty_tmp.exists(), "空 _tmp_ 目录未被删除")
        self.assertFalse(nested_tmp.exists(), "非空 _tmp_ 目录未被删除")

        # 验证内容被原子上移到 parent
        promoted_file = sandbox_out / "payload.txt"
        self.assertTrue(promoted_file.exists(), "payload.txt 未被解套上移")
        self.assertEqual(promoted_file.read_text(encoding="utf-8"), "safe content")

    def test_case_7_native_game_folder_stops_recursion_and_protects_deep_zips(self):
        """
        Case 7: 针对 yhsj 场景的绝对熔断测试：
        外层套娃解压出原生游戏本体文件夹 (Game_Cornelia_Ver1.12) 后，
        立即绝对熔断，严禁穿透递归进入内部，其深层存储的 676 个翻译工具备份 zip 绝不被解压破坏！
        """
        # 1. 构造深层备份 zip
        save1_path = self.test_dir / "temp_save01.zip"
        save2_path = self.test_dir / "temp_save02.zip"
        with zipfile.ZipFile(save1_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("save01.dat", "save data 1 payload")
        with zipfile.ZipFile(save2_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("save02.dat", "save data 2 payload")

        # 2. 构造套娃内层 inner_game.zip，包含游戏原生文件夹和深层备份 zip
        inner_zip = self.test_dir / "inner_game.zip"
        with zipfile.ZipFile(inner_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("Game_Cornelia_Ver1.12/Game.exe", "fake_game_exe_binary")
            zf.writestr("Game_Cornelia_Ver1.12/www/data/Actors.json", '{"actor": "Cornelia"}')
            zf.write(save1_path, arcname="Game_Cornelia_Ver1.12/MTool/Tool/gameSaveBackup/save01.zip")
            zf.write(save2_path, arcname="Game_Cornelia_Ver1.12/MTool/Tool/gameSaveBackup/save02.zip")

        # 3. 构造外层套娃包 case7_outer.zip
        outer_zip = self.test_dir / "case7_outer.zip"
        with zipfile.ZipFile(outer_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(inner_zip, arcname="inner_game.zip")

        # 清除临时文件
        save1_path.unlink()
        save2_path.unlink()
        inner_zip.unlink()

        # 4. 执行解压任务
        self.queue._process_task("task_c7", str(outer_zip))

        # 5. 验证交付目录
        dest_dir = self.test_dir / "case7_outer"
        self.assertTrue(dest_dir.exists(), f"交付目录不存在: {dest_dir}")

        game_folder = dest_dir / "Game_Cornelia_Ver1.12"
        self.assertTrue(game_folder.exists(), "游戏原生文件夹缺失")
        self.assertTrue(game_folder.is_dir(), "游戏原生文件夹不是目录")

        # 验证游戏本体主程序与数据文件完好
        game_exe = game_folder / "Game.exe"
        self.assertTrue(game_exe.exists(), "Game.exe 缺失")
        self.assertEqual(game_exe.read_text(encoding="utf-8"), "fake_game_exe_binary")

        actors_json = game_folder / "www" / "data" / "Actors.json"
        self.assertTrue(actors_json.exists(), "Actors.json 缺失")

        # 6. 【核心底线验证】深层备份 zip 必须完好无损地以 .zip 格式保留，绝对禁止被当成嵌套包解压！
        backup_dir = game_folder / "MTool" / "Tool" / "gameSaveBackup"
        self.assertTrue(backup_dir.exists(), "备份目录缺失")

        deep_zip1 = backup_dir / "save01.zip"
        deep_zip2 = backup_dir / "save02.zip"
        self.assertTrue(deep_zip1.exists(), "深层备份 save01.zip 被错误删除或解压")
        self.assertTrue(deep_zip1.is_file(), "深层备份 save01.zip 不是普通文件")
        self.assertTrue(deep_zip2.exists(), "深层备份 save02.zip 被错误删除或解压")
        self.assertTrue(deep_zip2.is_file(), "深层备份 save02.zip 不是普通文件")

        # 验证 zip 内部的文件绝未被泄漏/解包到备份目录下
        self.assertFalse((backup_dir / "save01.dat").exists(), "save01.dat 被错误解压到了备份目录中")
        self.assertFalse((backup_dir / "save02.dat").exists(), "save02.dat 被错误解压到了备份目录中")

        # 验证中间套娃 inner_game.zip 已被安全删除，无 _tmp_* 残留
        all_items = [p.name for p in dest_dir.iterdir()]
        self.assertNotIn("inner_game.zip", all_items)
        self.assertFalse(any(name.startswith("_tmp_") for name in all_items))

    def test_case_8_status_flow_unencrypted_archive_never_shows_verifying_password(self):
        """
        Case 8: 状态流转验证：
        无密码的压缩包在全量解压写盘与后续流程中，通知状态为“正在提取”，
        绝不长期悬停或错误显示为“验证密码”。
        """
        archive_path = self.test_dir / "case8_status.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("test.txt", "unencrypted content")

        recorded_statuses = []

        def on_status_changed(tid, fn, status, eng, rem):
            if tid == "task_c8":
                recorded_statuses.append(status)

        self.queue.task_status_changed.connect(on_status_changed)

        self.queue._process_task("task_c8", str(archive_path))

        # 验证状态流转：应该包含“正在提取”，并在最后达到“成功”
        self.assertIn("正在提取", recorded_statuses)
        self.assertEqual(recorded_statuses[-1], "成功")

        # 关键验证：无密码解压过程绝对不能发出“验证密码”状态
        self.assertNotIn("验证密码", recorded_statuses, f"无密码解压出现不合法的状态: {recorded_statuses}")


if __name__ == "__main__":
    unittest.main()
