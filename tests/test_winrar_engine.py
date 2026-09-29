"""
自动化验证测试：WinRAREngine 静默解压与密码分类验证
测试目标：
1. WinRAREngine 无密盲解加密包时，通过命令行参数 -p- 实现完全静默，绝不弹窗阻塞。
2. 退出码 11 精准分类：
   - 未传密码时触发 PasswordRequiredError；
   - 传了错误密码时触发 WrongPasswordError。
3. repair_extract 静默处理加密包。
"""
import os
import sys
import shutil
import subprocess
import unittest
from pathlib import Path

# 添加项目根目录到 sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.engine import WinRAREngine, WINRAR_PATHS, UNRAR_PATHS
from core.exceptions import PasswordRequiredError, WrongPasswordError


def _find_winrar_executable() -> str | None:
    """查找系统中可用的 WinRAR.exe。"""
    for p in WINRAR_PATHS + UNRAR_PATHS:
        if os.path.isfile(p):
            return p
    return None


WINRAR_EXE = _find_winrar_executable()


@unittest.skipUnless(WINRAR_EXE is not None, "系统中未检测到 WinRAR 引擎，跳过 WinRAR 专属测试")
class TestWinRAREngineSilent(unittest.TestCase):
    """验证 WinRAR 引擎静默执行与密码异常精准分类。"""

    @classmethod
    def setUpClass(cls):
        cls.winrar_path = WINRAR_EXE
        cls.test_dir = PROJECT_ROOT / "tests" / "test_scratch_winrar"
        cls.test_dir.mkdir(parents=True, exist_ok=True)

        cls.secret_content = "SuperSecretPayloadData_2026"
        cls.archive_password = "TestPassword123"

        # 准备待压缩的测试源文件
        src_file = cls.test_dir / "secret.txt"
        src_file.write_text(cls.secret_content, encoding="utf-8")

        # 使用 WinRAR 自身创建高强度加密 RAR 包 (加密头部与文件)
        cls.encrypted_rar = cls.test_dir / "encrypted.rar"
        subprocess.run(
            [
                cls.winrar_path,
                "a",
                "-ep",
                "-ibck",
                "-inul",
                f"-hp{cls.archive_password}",
                str(cls.encrypted_rar),
                str(src_file),
            ],
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )

    @classmethod
    def tearDownClass(cls):
        if cls.test_dir.exists():
            shutil.rmtree(cls.test_dir, ignore_errors=True)

    def setUp(self):
        self.engine = WinRAREngine(self.winrar_path)
        self.output_dir = self.test_dir / "out"
        if self.output_dir.exists():
            shutil.rmtree(self.output_dir, ignore_errors=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self.output_dir.exists():
            shutil.rmtree(self.output_dir, ignore_errors=True)

    def test_blind_extract_no_password_raises_password_required_silent(self):
        """无密盲解 (password=None 或 '') 必须静默退出并抛出 PasswordRequiredError，绝不弹窗阻塞。"""
        # 测试 password=None
        with self.assertRaises(PasswordRequiredError) as ctx:
            self.engine.extract(self.encrypted_rar, self.output_dir, password=None, timeout=10)
        self.assertIn("11", ctx.exception.engine_output)

        # 测试 password=""
        with self.assertRaises(PasswordRequiredError) as ctx_empty:
            self.engine.extract(self.encrypted_rar, self.output_dir, password="", timeout=10)
        self.assertIn("11", ctx_empty.exception.engine_output)

    def test_extract_with_wrong_password_raises_wrong_password_error(self):
        """传入错误密码时，必须抛出 WrongPasswordError。"""
        with self.assertRaises(WrongPasswordError) as ctx:
            self.engine.extract(
                self.encrypted_rar,
                self.output_dir,
                password="IncorrectPassword999",
                timeout=10,
            )
        self.assertIn("11", ctx.exception.engine_output)
        self.assertEqual(ctx.exception.password_used, "IncorrectPassword999")

    def test_extract_with_correct_password_succeeds(self):
        """传入正确密码时，解压成功并正确还原文件内容。"""
        dest = self.engine.extract(
            self.encrypted_rar,
            self.output_dir,
            password=self.archive_password,
            timeout=10,
        )
        extracted_file = Path(dest) / "secret.txt"
        self.assertTrue(extracted_file.exists(), f"解压目标文件不存在: {extracted_file}")
        self.assertEqual(extracted_file.read_text(encoding="utf-8"), self.secret_content)

    def test_repair_extract_no_password_silent(self):
        """repair_extract 未传密码时同样附带静默逻辑，遇到加密包抛出 PasswordRequiredError。"""
        with self.assertRaises(PasswordRequiredError) as ctx:
            self.engine.repair_extract(
                self.encrypted_rar,
                self.output_dir,
                password=None,
                timeout=10,
            )
        self.assertIn("11", ctx.exception.engine_output)


if __name__ == "__main__":
    unittest.main()
