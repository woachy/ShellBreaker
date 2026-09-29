"""
解压引擎抽象层 — 铁律二：黑盒防注入
支持 Bandizip (bz.exe) 和 WinRAR/UnRAR。
阶段 6.1：手动指定路径优先 + 柔性降级（找不到引擎不崩溃）。
"""
import subprocess
import logging
import os
from pathlib import Path
from enum import Enum, auto
from typing import Optional

from core.exceptions import (
    EngineNotFoundError,
    PasswordRequiredError,
    WrongPasswordError,
    ExtractionFailedError,
)

logger = logging.getLogger("ShellBreaker.Engine")


class EngineType(Enum):
    BANDIZIP = auto()
    WINRAR = auto()


# Bandizip 安装目录（用于递归搜索 bz.exe，绝不用 GUI exe 路径）
BANDIZIP_INSTALL_ROOTS = [
    r"C:\Program Files\Bandizip",
    r"C:\Program Files (x86)\Bandizip",
    r"C:\Bandizip",
]

WINRAR_PATHS = [
    r"C:\Program Files\WinRAR\WinRAR.exe",
    r"C:\Program Files (x86)\WinRAR\WinRAR.exe",
    r"C:\WinRAR\WinRAR.exe",
]

UNRAR_PATHS = [
    r"C:\Program Files\WinRAR\UnRAR.exe",
    r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
]


class BaseEngine:
    """解压引擎抽象基类。"""

    def __init__(self, exe_path: str | Path):
        self.exe_path = str(Path(exe_path).resolve())
        if not os.path.isfile(self.exe_path):
            raise EngineNotFoundError(self.exe_path)

    def extract(
        self,
        archive: str | Path,
        dest: str | Path,
        password: Optional[str] = None,
        timeout: int = 300,
    ) -> Path:
        raise NotImplementedError

    def repair_extract(
        self,
        archive: str | Path,
        dest: str | Path,
        password: Optional[str] = None,
        timeout: int = 600,
    ) -> Path:
        return self.extract(archive, dest, password=password, timeout=timeout)

    def _run_command(self, cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
        logger.debug(f"执行命令: {cmd}")
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            logger.debug(f"引擎退出码: {result.returncode}")
            if result.stdout:
                logger.debug(f"引擎 stdout (前200): {result.stdout[:200]}")
            if result.stderr:
                logger.debug(f"引擎 stderr (前200): {result.stderr[:200]}")
            return result
        except subprocess.TimeoutExpired as e:
            raise ExtractionFailedError(str(cmd[-1] if cmd else "?"), -1, f"解压超时 ({timeout}s)")

    @staticmethod
    def _classify_result(
        result: subprocess.CompletedProcess,
        archive_str: str,
        password_was_provided: bool,
        password_used: str = "",
    ) -> None:
        """
        精准三分异常——绝不允许密码问题被当成文件损坏进入 Fallback 链。

        优先级：
        1. 密码错误 → WrongPasswordError（阻止 Fallback）
        2. 需要密码但未提供 → PasswordRequiredError（触发密码弹窗）
        3. 文件损坏/格式错误 → ExtractionFailedError（允许 Fallback）
        """
        combined = (result.stdout + result.stderr).lower()

        # --- 第一优先级：密码错误关键词 ---
        wrong_pwd_kw = [
            "wrong password", "incorrect password",
            "password incorrect", "bad password",
            "密码错误", "密码不正确",
        ]
        if any(kw in combined for kw in wrong_pwd_kw):
            raise WrongPasswordError(archive_str, password_used, result.stderr or result.stdout)

        # --- 第二优先级：需要密码关键词 ---
        need_pwd_kw = [
            "password", "encrypted", "密码", "加密",
            "no password", "missing password",
        ]
        is_encrypted = any(kw in combined for kw in need_pwd_kw)

        if password_was_provided and is_encrypted:
            # 传了密码但引擎仍然报需要密码 → 密码错误
            raise WrongPasswordError(archive_str, password_used, result.stderr or result.stdout)

        if is_encrypted:
            # 未传密码但引擎报需要密码
            raise PasswordRequiredError(archive_str, result.stderr or result.stdout)

        # --- 第三优先级：非零退出 → 文件损坏 ---
        if result.returncode != 0:
            raise ExtractionFailedError(archive_str, result.returncode, result.stderr)


class BandizipEngine(BaseEngine):
    """Bandizip (bz.exe) 引擎。"""

    def extract(
        self,
        archive: str | Path,
        dest: str | Path,
        password: Optional[str] = None,
        timeout: int = 300,
    ) -> Path:
        archive_str = str(Path(archive).resolve())
        dest_str = str(Path(dest).resolve())
        os.makedirs(dest_str, exist_ok=True)

        # 铁律二：纯 List 传参
        cmd = [
            self.exe_path, "x",
            "-o:" + dest_str, "-y",
            archive_str,
        ]
        pw_provided = bool(password)
        if password:
            cmd.insert(-1, "-p:" + password)

        result = self._run_command(cmd, timeout)

        if result.returncode != 0:
            self._classify_result(result, archive_str, pw_provided, password or "")

        logger.info(f"Bandizip 解压完成: {archive_str} → {dest_str}")
        return Path(dest_str)

    def repair_extract(
        self,
        archive: str | Path,
        dest: str | Path,
        password: Optional[str] = None,
        timeout: int = 600,
    ) -> Path:
        archive_str = str(Path(archive).resolve())
        dest_str = str(Path(dest).resolve())
        os.makedirs(dest_str, exist_ok=True)

        cmd = [
            self.exe_path, "x",
            "-o:" + dest_str, "-y", "-aoa",
            archive_str,
        ]
        pw_provided = bool(password)
        if password:
            cmd.insert(-1, "-p:" + password)

        result = self._run_command(cmd, timeout)

        if result.returncode != 0:
            self._classify_result(result, archive_str, pw_provided, password or "")

        logger.info(f"Bandizip repair 提取完成: {archive_str} → {dest_str}")
        return Path(dest_str)


class WinRAREngine(BaseEngine):
    """WinRAR 引擎 — 静默命令行模式，调用 WinRAR.exe 全兼容。"""

    def extract(
        self,
        archive: str | Path,
        dest: str | Path,
        password: Optional[str] = None,
        timeout: int = 300,
    ) -> Path:
        archive_str = str(Path(archive).resolve())
        dest_str = str(Path(dest).resolve())
        os.makedirs(dest_str, exist_ok=True)

        cmd = [
            self.exe_path, "x", "-ibck", "-inul", "-y",
            archive_str, dest_str + os.sep,
        ]
        pw_provided = bool(password)
        if password:
            cmd.insert(-2, f"-p{password}")
        else:
            cmd.insert(-2, "-p-")

        result = self._run_command(cmd, timeout)

        if result.returncode != 0:
            self._classify_exit_code(result, archive_str, pw_provided, password or "")

        logger.info(f"WinRAR 解压完成: {archive_str} → {dest_str}")
        return Path(dest_str)

    @staticmethod
    def _classify_exit_code(
        result: subprocess.CompletedProcess,
        archive_str: str,
        pw_provided: bool,
        password_used: str = "",
    ) -> None:
        """
        WinRAR 错误码精确分类，优先级高于文本解析。
        退出码：0=成功 1=警告 2=致命 3=CRC 5=写入 6=打开
               7=命令行 8=内存 9=创建 10=无匹配 11=密码错误 255=中断
        """
        code = result.returncode
        if code == 11:
            if pw_provided:
                raise WrongPasswordError(
                    archive_str, password_used,
                    f"WinRAR exit code {code}: 密码错误 | {result.stderr or ''}")
            else:
                raise PasswordRequiredError(
                    archive_str,
                    f"WinRAR exit code {code}: 压缩包已加密需要密码 | {result.stderr or ''}")
        if code == 1:
            logger.warning(
                "WinRAR 警告 (exit=%d): %s | %s",
                code, archive_str, (result.stderr or result.stdout or ""))
            return
        if code in (3, 5, 6, 7, 8, 9, 10, 255):
            raise ExtractionFailedError(
                archive_str, code,
                f"WinRAR exit code {code}: {result.stderr or result.stdout or ''}")

        BaseEngine._classify_result(result, archive_str, pw_provided, password_used)

    def repair_extract(
        self,
        archive: str | Path,
        dest: str | Path,
        password: Optional[str] = None,
        timeout: int = 600,
    ) -> Path:
        archive_path = Path(archive).resolve()
        archive_str = str(archive_path)
        dest_str = str(Path(dest).resolve())

        repaired_dir = archive_path.parent / "_repaired"
        os.makedirs(str(repaired_dir), exist_ok=True)

        repair_cmd = [
            self.exe_path, "r", "-ibck", "-inul", "-y",
            archive_str, str(repaired_dir) + os.sep,
        ]
        if password:
            repair_cmd.insert(-2, f"-p{password}")
        else:
            repair_cmd.insert(-2, "-p-")
        logger.info(f"WinRAR 执行修复: {repair_cmd}")
        self._run_command(repair_cmd, timeout=min(timeout, 300))

        repaired_archive = None
        for f in repaired_dir.iterdir():
            if f.is_file() and f.suffix.lower() in {".rar", ".zip"}:
                repaired_archive = f
                break

        if repaired_archive is None:
            logger.warning("WinRAR repair 未生成修复文件，降级为普通提取")
            return self.extract(archive_str, dest_str, password=password, timeout=timeout)

        logger.info(f"WinRAR 修复文件: {repaired_archive}")
        return self.extract(str(repaired_archive), dest_str, password=password, timeout=timeout)


# ---------- 引擎工厂 ----------

def _find_bandizip_cli() -> str | None:
    """
    定位 Bandizip CLI 内核 (bz.exe)。
    递归搜索 Bandizip 安装目录，绝不在 subprocess 中调用 GUI 版。
    若找不到 bz.exe，尝试 PATH 环境变量。
    """
    for root in BANDIZIP_INSTALL_ROOTS:
        if not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            if "bz.exe" in filenames:
                bz_path = os.path.join(dirpath, "bz.exe")
                logger.info(f"发现 Bandizip CLI（递归搜索）: {bz_path}")
                return bz_path

    import shutil as _shutil
    found = _shutil.which("bz.exe")
    if found:
        logger.info(f"发现 Bandizip CLI（PATH）: {found}")
        return found

    logger.error("未找到 bz.exe，Bandizip CLI 内核缺失。")
    return None


def discover_engine() -> tuple[Optional[EngineType], Optional[str]]:
    """
    四级柔性引擎发现（阶段 6.4）。

    优先级：
    1. 用户偏好引擎的手动路径有效 → 使用该引擎
    2. 偏好引擎路径无效但另一引擎手动路径有效 → 降级使用另一引擎
    3. 两路径都空 → 自动扫描（优先扫描偏好引擎）
    4. 都找不到 → 返回 (None, None)，绝不 raise

    返回 (engine_type, exe_path) 或 (None, None)。
    """
    from data.config_manager import ConfigManager
    cfg = ConfigManager()

    default_engine = cfg.get("default_engine", "bandizip")
    bz_path = cfg.get("bandizip_path", "")
    wr_path = cfg.get("winrar_path", "")

    # --- 第一优先级：用户手动指定路径（按偏好引擎优先）---
    if default_engine == "bandizip":
        if bz_path and os.path.isfile(bz_path):
            logger.info(f"使用用户配置的 Bandizip 路径: {bz_path}")
            return EngineType.BANDIZIP, bz_path
        if wr_path and os.path.isfile(wr_path):
            logger.info(f"偏好引擎(Bandizip)路径无效，降级使用 WinRAR: {wr_path}")
            return EngineType.WINRAR, wr_path
    else:  # winrar
        if wr_path and os.path.isfile(wr_path):
            logger.info(f"使用用户配置的 WinRAR 路径: {wr_path}")
            return EngineType.WINRAR, wr_path
        if bz_path and os.path.isfile(bz_path):
            logger.info(f"偏好引擎(WinRAR)路径无效，降级使用 Bandizip: {bz_path}")
            return EngineType.BANDIZIP, bz_path

    # --- 第二优先级：自动扫描（按偏好引擎优先）---
    if default_engine == "bandizip":
        bz = _find_bandizip_cli()
        if bz:
            return EngineType.BANDIZIP, bz
        for p in WINRAR_PATHS:
            if os.path.isfile(p):
                logger.info(f"发现 WinRAR 引擎: {p}")
                return EngineType.WINRAR, p
        for p in UNRAR_PATHS:
            if os.path.isfile(p):
                logger.info(f"发现 UnRAR 引擎: {p}")
                return EngineType.WINRAR, p
    else:  # winrar
        for p in WINRAR_PATHS:
            if os.path.isfile(p):
                logger.info(f"发现 WinRAR 引擎: {p}")
                return EngineType.WINRAR, p
        for p in UNRAR_PATHS:
            if os.path.isfile(p):
                logger.info(f"发现 UnRAR 引擎: {p}")
                return EngineType.WINRAR, p
        bz = _find_bandizip_cli()
        if bz:
            return EngineType.BANDIZIP, bz

    # --- 第三优先级：什么都找不到 → 柔性降级，不崩溃 ---
    logger.warning("未找到任何解压引擎，请前往全局设置手动选择引擎路径")
    return None, None


def create_engine(exe_path: Optional[str], engine_type: Optional[EngineType]) -> Optional[BaseEngine]:
    """创建引擎实例。若 exe_path 或 engine_type 为 None，返回 None。"""
    if exe_path is None or engine_type is None:
        return None
    if engine_type == EngineType.BANDIZIP:
        return BandizipEngine(exe_path)
    else:
        return WinRAREngine(exe_path)


def get_engine_display_name(engine_type: Optional[EngineType]) -> str:
    """获取引擎显示名称。None 时返回提示文本。"""
    if engine_type is None:
        return "未配置 - 请前往全局设置手动选择引擎路径"
    return "Bandizip (bz.exe)" if engine_type == EngineType.BANDIZIP else "WinRAR"