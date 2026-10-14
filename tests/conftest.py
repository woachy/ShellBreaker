"""
Pytest 全局配置与 Qt Application 固化。
确保整个测试套件生命周期使用 QApplication，支持 QWidget 与 QCore 信号。
"""
import os
import sys

# 无头/CI 环境强制 offscreen 渲染，避免 C++ / Shiboken 崩溃
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

# 全局初始化 QApplication 实例
_app = QApplication.instance() or QApplication(sys.argv)
