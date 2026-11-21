"""
Pytest 全局配置与 Qt Application 固化。
确保整个测试套件生命周期使用 QApplication，支持 QWidget 与 QCore 信号，
并在每条用例结束后于主线程回收离屏 QWidget 循环引用，避免工作线程触发 GC 跨线程析构 Qt 对象。
"""
import gc
import os
import sys

import pytest

# 无头/CI 环境强制 offscreen 渲染，避免 C++ / Shiboken 崩溃
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

# 全局初始化 QApplication 实例
_app = QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(autouse=True)
def _collect_qt_garbage_on_gui_thread():
    yield
    gc.collect()
    app = QApplication.instance()
    if app is not None:
        app.processEvents()
