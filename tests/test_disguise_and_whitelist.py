"""
自动化验证测试：伪装包识别、形态驱动终点判定与 APK 白名单保护测试
对应需求：
1. APK 终点白名单保护：.apk (及 .exe, .msi) 无论内部结构是不是 ZIP，必须作为绝对终点，严禁当作压缩包继续拆解；
2. 形态驱动的伪装检测（精准狙击单一大文件伪装，绝对不碰多文件业务内容）：
   - 当 base 目录已经解压出子目录或多个文件（Data Body）时：仅处理标准压缩包，绝不对散装媒体进行伪装下钻，立即视为终点；
   - 当 base 目录仅包含“单个独立孤儿大文件”（或附带 .txt/.url/.nfo 说明文件）：
     * 检查伪装后缀（.jpg, .png, .mp4 等）且大小达到阈值（> 10MB）；
     * 验证前 16 字节魔数：匹配则原位重命名为对应真实后缀 (零物理复制开销) 并解压提权；不匹配则不碰，作为终点交付。
3. 密码库遍历自学习保持完整。

用例覆盖：
- Case 1: 嵌套解压产出单个伪装成 .jpg 的压缩包（内部含游戏文件夹），验证成功识别、剥离并解出文件夹；
- Case 2: 嵌套解压产出 .apk 文件（内部即使是 ZIP），验证绝对不被解压，保持 .apk 完整；
- Case 3: 嵌套解压产出包含多个真实 .jpg 图片的文件夹，验证所有图片完好无损，停止判定瞬间生效；
- Case 4: 嵌套解压产出单个真实 .mp4 视频，验证魔数不匹配，直接交付，不误解；
- Case 5: 验证 detect_archive_type 对各类魔数与受保护终点白名单的底层准确性。
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
from core.sandbox import (
    SandboxManager,
    PROTECTED_ENDPOINT_EXTS,
    POTENTIAL_DISGUISE_EXTS,
    ACCESSORY_EXTS,
    detect_archive_type,
)
from data.config_manager import ConfigManager


class TestDisguiseAndWhitelist(unittest.TestCase):
    """验证伪装包识别与 APK/可执行程序终点白名单保护的自动化测试。"""

    def setUp(self):
        self.test_dir = PROJECT_ROOT / "tests" / "test_scratch_disguise"
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)
        self.test_dir.mkdir(parents=True, exist_ok=True)

        self.cfg = ConfigManager()
        self._orig_output_mode = self.cfg.get("output_mode")
        self._orig_custom_path = self.cfg.get("custom_output_path")
        self._orig_disguise_mb = self.cfg.get("disguise_min_size_mb")

        self.cfg.set("output_mode", "source_directory")
        self.cfg.set("custom_output_path", str(self.test_dir))
        self.cfg.set("disguise_min_size_mb", 10)

        self.queue = TaskQueue()
        self.queue._ensure_engine()
        self.assertIsNotNone(self.queue._engine, "测试需要可用的解压引擎 (Bandizip 或 WinRAR)")

    def tearDown(self):
        self.cfg.set("output_mode", self._orig_output_mode)
        self.cfg.set("custom_output_path", self._orig_custom_path)
        if self._orig_disguise_mb is not None:
            self.cfg.set("disguise_min_size_mb", self._orig_disguise_mb)
        if self.test_dir.exists():
            shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_case_1_nested_single_disguised_jpg_peeled_successfully(self):
        """
        Case 1: 嵌套解压产出单个伪装成 .jpg 的压缩包（内部含游戏文件夹），
        验证成功识别魔数、原位剥离外壳并最终解出游戏文件夹。
        """
        # 1. 构造内部伪装包 game_disguised.jpg (实际是 ZIP，且体积 > 10MB)
        disguised_jpg = self.test_dir / "game_disguised.jpg"
        game_payload_text = "Executable binary data for Game.exe"
        # 写入 11MB 数据以达到并超过 10MB 伪装阈值
        dummy_large_data = b"X" * (11 * 1024 * 1024)

        with zipfile.ZipFile(disguised_jpg, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr("GameFolder/Game.exe", game_payload_text)
            zf.writestr("GameFolder/data.pak", dummy_large_data)

        self.assertGreater(disguised_jpg.stat().st_size, 10 * 1024 * 1024)

        # 2. 构造外层压缩包 case1_outer.zip，内部包含伪装包和说明文件 readme.txt
        outer_zip = self.test_dir / "case1_outer.zip"
        with zipfile.ZipFile(outer_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(disguised_jpg, arcname="game_disguised.jpg")
            zf.writestr("readme.txt", "Please enjoy the game!")

        # 清除源中间文件
        disguised_jpg.unlink()

        # 3. 执行任务
        self.queue._process_task("task_dw_1", str(outer_zip))

        # 4. 验证交付路径 (第 1 层：case1_outer)
        dest_dir = self.test_dir / "case1_outer"
        self.assertTrue(dest_dir.exists(), f"交付目录不存在: {dest_dir}")

        # 5. 验证伪装包已被成功解压与剥离：
        # - GameFolder 存在且位于交付目录下
        # - GameFolder 内部文件完整
        # - 伪装的 .jpg 与重命名的 .zip 中间文件已被彻底删除
        game_folder = dest_dir / "GameFolder"
        self.assertTrue(game_folder.exists(), "游戏文件夹 GameFolder 未解出")
        self.assertTrue(game_folder.is_dir(), "GameFolder 不是文件夹")

        game_exe = game_folder / "Game.exe"
        self.assertTrue(game_exe.exists(), "Game.exe 未解出")
        self.assertEqual(game_exe.read_text(encoding="utf-8"), game_payload_text)

        game_data = game_folder / "data.pak"
        self.assertTrue(game_data.exists(), "data.pak 未解出")
        self.assertEqual(game_data.stat().st_size, len(dummy_large_data))

        # 说明文件依然保留
        readme = dest_dir / "readme.txt"
        self.assertTrue(readme.exists(), "readme.txt 缺失")

        # 伪装包已被彻底删除，无任何残存
        self.assertFalse((dest_dir / "game_disguised.jpg").exists(), "中间伪装包 game_disguised.jpg 未删除")
        self.assertFalse((dest_dir / "game_disguised.zip").exists(), "中间压缩包 game_disguised.zip 未删除")

        # 验证无任何 _tmp_* 目录
        all_items = [p.name for p in dest_dir.iterdir()]
        self.assertFalse(any(name.startswith("_tmp_") for name in all_items), f"存在 _tmp_ 残留: {all_items}")

    def test_case_2_nested_apk_whitelist_protection_intact(self):
        """
        Case 2: 嵌套解压产出 .apk 文件（内部即使是标准的 ZIP 结构），
        验证受保护终点白名单生效，绝对不被拆解，保持 .apk 完整交付。
        """
        # 1. 构造一个合法的 APK 文件 (内部是标准的 ZIP 结构)
        inner_apk = self.test_dir / "mobile_app.apk"
        manifest_content = "<manifest xmlns:android='http://schemas.android.com/apk/res/android'/>"
        dex_content = b"dex\n035\x00sample_dex_code"

        with zipfile.ZipFile(inner_apk, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("AndroidManifest.xml", manifest_content)
            zf.writestr("classes.dex", dex_content)

        # 2. 构造外层压缩包 case2_outer.zip 包含该 APK
        outer_zip = self.test_dir / "case2_outer.zip"
        with zipfile.ZipFile(outer_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(inner_apk, arcname="mobile_app.apk")
        inner_apk.unlink()

        # 3. 执行任务
        self.queue._process_task("task_dw_2", str(outer_zip))

        # 4. 验证交付路径
        dest_dir = self.test_dir / "case2_outer"
        self.assertTrue(dest_dir.exists(), f"交付目录不存在: {dest_dir}")

        # 5. 验证 APK 文件保持完整，绝未被当成 ZIP 拆解
        delivered_apk = dest_dir / "mobile_app.apk"
        self.assertTrue(delivered_apk.exists(), "APK 文件不存在于交付目录中")

        # 验证内部未被拆解至外层
        self.assertFalse((dest_dir / "AndroidManifest.xml").exists(), "APK 被非法拆解：AndroidManifest.xml 泄露到外部")
        self.assertFalse((dest_dir / "classes.dex").exists(), "APK 被非法拆解：classes.dex 泄露到外部")

        # 验证交付的 APK 仍是一个可正常读取的有效 APK/ZIP
        with zipfile.ZipFile(delivered_apk, "r") as zf:
            self.assertEqual(zf.read("AndroidManifest.xml").decode("utf-8"), manifest_content)
            self.assertEqual(zf.read("classes.dex"), dex_content)

    def test_case_3_nested_multiple_real_jpgs_stopping_condition(self):
        """
        Case 3: 嵌套解压产出包含多个真实 .jpg 图片的文件夹（图集/媒体资源 Data Body），
        验证所有图片完好无损，停止判定瞬间生效，绝不误触伪装下钻。
        """
        # 构造真实的 JPEG 文件二进制数据 (以 FF D8 FF 开头)
        jpeg_header = b"\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00"
        img1_data = jpeg_header + b"Photo 1 Binary Content " * 50
        img2_data = jpeg_header + b"Photo 2 Binary Content " * 50
        img3_data = jpeg_header + b"Photo 3 Binary Content " * 50

        # 构造外层压缩包
        outer_zip = self.test_dir / "case3_outer.zip"
        with zipfile.ZipFile(outer_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("photo_01.jpg", img1_data)
            zf.writestr("photo_02.jpg", img2_data)
            zf.writestr("photo_03.jpg", img3_data)
            zf.writestr("info.txt", "Image gallery description")

        # 执行任务
        self.queue._process_task("task_dw_3", str(outer_zip))

        # 验证交付结果
        dest_dir = self.test_dir / "case3_outer"
        self.assertTrue(dest_dir.exists(), f"交付目录不存在: {dest_dir}")

        f1 = dest_dir / "photo_01.jpg"
        f2 = dest_dir / "photo_02.jpg"
        f3 = dest_dir / "photo_03.jpg"
        self.assertTrue(f1.exists(), "photo_01.jpg 丢失")
        self.assertTrue(f2.exists(), "photo_02.jpg 丢失")
        self.assertTrue(f3.exists(), "photo_03.jpg 丢失")

        # 验证图片内容完全未受损
        self.assertEqual(f1.read_bytes(), img1_data)
        self.assertEqual(f2.read_bytes(), img2_data)
        self.assertEqual(f3.read_bytes(), img3_data)

    def test_case_4_nested_single_real_mp4_magic_mismatch_delivered(self):
        """
        Case 4: 嵌套解压产出单个真实 .mp4 视频（即使 > 10MB），
        验证魔数不匹配（ftyp 非 PK/Rar/7z），直接终点交付，绝不误解。
        """
        # 构造真实 MP4 文件头与数据 (以 \x00\x00\x00\x20ftyp 开头，> 10MB)
        mp4_header = b"\x00\x00\x00\x20ftypisom\x00\x00\x02\x00isomiso2mp41"
        large_video_data = mp4_header + b"\x00" * (11 * 1024 * 1024)

        outer_zip = self.test_dir / "case4_outer.zip"
        video_name = "sample_movie.mp4"
        with zipfile.ZipFile(outer_zip, "w", zipfile.ZIP_STORED) as zf:
            zf.writestr(video_name, large_video_data)
            zf.writestr("movie.nfo", "Video metadata info")

        # 执行任务
        self.queue._process_task("task_dw_4", str(outer_zip))

        # 验证交付结果
        dest_dir = self.test_dir / "case4_outer"
        self.assertTrue(dest_dir.exists(), f"交付目录不存在: {dest_dir}")

        delivered_video = dest_dir / video_name
        self.assertTrue(delivered_video.exists(), f"{video_name} 丢失")
        self.assertEqual(delivered_video.stat().st_size, len(large_video_data))
        self.assertEqual(delivered_video.read_bytes()[:32], large_video_data[:32])

        delivered_nfo = dest_dir / "movie.nfo"
        self.assertTrue(delivered_nfo.exists(), "movie.nfo 丢失")

    def test_case_5_detect_archive_type_magics_and_whitelist(self):
        """
        Case 5: 单元级验证 detect_archive_type 函数的魔数识别与白名单过滤。
        """
        scratch = self.test_dir / "magic_check"
        scratch.mkdir(parents=True, exist_ok=True)

        # 1. ZIP 魔数 (PK\x03\x04)
        zip_fake = scratch / "fake.jpg"
        zip_fake.write_bytes(b"PK\x03\x04" + b"\x00" * 20)
        self.assertEqual(detect_archive_type(zip_fake), ".zip")

        # 2. RAR 魔数 (Rar!\x1a\x07)
        rar_fake = scratch / "fake.png"
        rar_fake.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 20)
        self.assertEqual(detect_archive_type(rar_fake), ".rar")

        # 3. 7Z 魔数 (7z\xbc\xaf'\x1c)
        sevenz_fake = scratch / "fake.bin"
        sevenz_fake.write_bytes(b"\x37\x7a\xbc\xaf\x27\x1c" + b"\x00" * 20)
        self.assertEqual(detect_archive_type(sevenz_fake), ".7z")

        # 4. 真实 JPEG (\xFF\xD8\xFF)
        real_jpg = scratch / "real.jpg"
        real_jpg.write_bytes(b"\xFF\xD8\xFF\xE0\x00\x10JFIF" + b"\x00" * 20)
        self.assertIsNone(detect_archive_type(real_jpg))

        # 5. 真实 MP4 (....ftyp)
        real_mp4 = scratch / "real.mp4"
        real_mp4.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"\x00" * 20)
        self.assertIsNone(detect_archive_type(real_mp4))

        # 6. 受保护终点白名单文件 (.apk, .exe, .msi) 无论内容是什么均返回 None
        apk_file = scratch / "app.apk"
        apk_file.write_bytes(b"PK\x03\x04" + b"\x00" * 20)
        self.assertIsNone(detect_archive_type(apk_file), ".apk 必须被白名单保护，返回 None")

        exe_file = scratch / "program.exe"
        exe_file.write_bytes(b"PK\x03\x04" + b"\x00" * 20)
        self.assertIsNone(detect_archive_type(exe_file), ".exe 必须被白名单保护，返回 None")

        msi_file = scratch / "setup.msi"
        msi_file.write_bytes(b"PK\x03\x04" + b"\x00" * 20)
        self.assertIsNone(detect_archive_type(msi_file), ".msi 必须被白名单保护，返回 None")


if __name__ == "__main__":
    unittest.main()
