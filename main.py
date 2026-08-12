"""
懒人解压😋 (ShellBreaker) — 程序入口
负责全局日志配置、异常兜底、样式加载，启动主窗口。
"""
import sys
import os
import logging
import traceback
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox
from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon

# --------------- 确保 logs 目录存在 ---------------
if getattr(sys, "frozen", False):
    _BASE_DIR = Path(sys.executable).parent
else:
    _BASE_DIR = Path(__file__).resolve().parent

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
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
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


# --------------- 启动入口 ---------------
def main():
    logger.info("=" * 50)
    logger.info("懒人解压😋 (ShellBreaker) 启动")
    logger.info(f"日志文件: {LOG_FILE}")

    # 高 DPI 支持
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("懒人解压😋")

    # ---------- 加载全局样式 (QSS) ----------
    qss_path = _BASE_DIR / "assets" / "style.qss"
    if qss_path.exists():
        app.setStyleSheet(qss_path.read_text(encoding="utf-8"))
        logger.info(f"QSS 样式表已加载: {qss_path}")
    else:
        logger.warning(f"未找到 QSS 样式表，使用默认外观: {qss_path}")

    # ---------- 设置应用图标 ----------
    icon_path = _BASE_DIR / "assets" / "app_icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
        logger.info(f"应用图标已加载: {icon_path}")

    # 延迟导入主窗口，避免循环依赖
    from ui.main_window import MainWindow

    window = MainWindow()
    window.show()

    logger.info("主窗口已显示")
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
