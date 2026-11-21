"""
便捷解压 (ShellBreaker) — 程序入口
负责全局日志配置、异常兜底、样式加载，启动主窗口。
"""
import sys
import os
import logging
import logging.handlers
import traceback
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QIcon

# --------------- 确保 logs 目录存在 ---------------
if getattr(sys, "frozen", False):
    _BASE_DIR = Path(sys.executable).parent
else:
    _BASE_DIR = Path(__file__).resolve().parent

# --------------- 资源目录解析 ---------------
# PyInstaller 单文件模式（onefile）下，datas 会解包到临时目录 sys._MEIPASS；
# onedir / 源码模式下资源就在项目 assets 目录。两者都探测，保证任意打包方式都能加载。
if getattr(sys, "frozen", False):
    _ASSET_DIRS = [
        Path(getattr(sys, "_MEIPASS", "")) / "assets",
        _BASE_DIR / "assets",
    ]
else:
    _ASSET_DIRS = [_BASE_DIR / "assets"]


def _find_asset(filename: str):
    for d in _ASSET_DIRS:
        p = d / filename
        if p.exists():
            return p
    return None

LOG_DIR = _BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# --------------- 全局 logging 配置 ---------------
LOG_FILE = LOG_DIR / "app.log"
LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

logging.basicConfig(
    level=logging.DEBUG,
    format=LOG_FORMAT,
    datefmt=LOG_DATEFMT,
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_FILE,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        ),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger("ShellBreaker")


# --------------- 全局防崩溃兜底 ---------------
def _global_exception_hook(exc_type, exc_value, exc_tb):
    """捕获所有未处理的异常，写入日志并弹窗，绝不允许主程序闪退。"""
    tb_text = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
    logger.critical(f"未捕获的异常:\n{tb_text}")

    # 如果已有 QApplication 实例，弹窗提示
    app = QApplication.instance()
    if app is not None:
        QMessageBox.critical(
            None,
            "严重错误",
            f"程序发生未预期的错误，已记录到日志文件。\n\n{tb_text[-500:]}",
            QMessageBox.StandardButton.Ok,
        )
    else:
        # 兜底：标准错误输出
        print(f"[FATAL] 未捕获异常:\n{tb_text}", file=sys.stderr)


sys.excepthook = _global_exception_hook


def extract_cli_paths(argv: list[str]) -> list[str]:
    """从命令行参数提取有效路径列表（跳过 argv[0]、空白参数与以 '-' 开头的选项）。"""
    paths: list[str] = []
    seen: set[str] = set()
    for raw in (argv[1:] if len(argv) > 1 else []):
        text = str(raw or "").strip()
        if not text or text.startswith("-"):
            continue
        if len(text) >= 2 and text[0] == text[-1] and text[0] in ('"', "'"):
            text = text[1:-1].strip()
        if not text or text.startswith("-"):
            continue
        normalized = os.path.abspath(os.path.expanduser(text))
        key = os.path.normcase(normalized)
        if key not in seen:
            seen.add(key)
            paths.append(normalized)
    return paths


# --------------- 启动入口 ---------------
def main():
    logger.info("=" * 50)
    logger.info("便捷解压 (ShellBreaker) 启动")
    logger.info(f"日志文件: {LOG_FILE}")

    # 启动自愈：还原异常中断遗留的伪装重命名文件
    try:
        from core.sandbox import recover_interrupted_renames
        recovered = recover_interrupted_renames()
        if recovered > 0:
            logger.info(f"启动自愈：已还原 {recovered} 个异常中断的伪装重命名文件")
    except Exception as e:
        logger.warning(f"启动自愈检查异常（已忽略，不影响程序启动）: {e}")

    # 高 DPI 支持
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("便捷解压")

    # ---------- 命令行参数解析与单实例 IPC 转发 ----------
    cli_paths = extract_cli_paths(sys.argv)
    from core.system_integration import SingleInstanceBridge

    bridge = SingleInstanceBridge()
    if bridge.try_forward_to_existing(cli_paths):
        logger.info("已将请求转发至运行中的 ShellBreaker 实例，当前进程退出")
        sys.exit(0)
    bridge.start_server()
    app.aboutToQuit.connect(bridge.stop_server)

    # ---------- 加载全局主题与样式 (V2 Fluent) ----------
    from ui.theme.theme_manager import ensure_theme
    ensure_theme(app)

    # ---------- 设置应用图标 ----------
    icon_path = _find_asset("app_icon.png")
    if icon_path is not None:
        app.setWindowIcon(QIcon(str(icon_path)))
        logger.info(f"应用图标已加载: {icon_path}")

    # 延迟导入主窗口，避免循环依赖
    from ui.main_window import MainWindow

    window = MainWindow()
    bridge.paths_received.connect(window.enqueue_cli_paths)
    window.show()

    if cli_paths:
        QTimer.singleShot(0, lambda: window.enqueue_cli_paths(cli_paths))

    logger.info("主窗口已显示")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
