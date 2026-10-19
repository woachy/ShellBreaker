"""设置对话框（兼容层）：内嵌 SettingsPage，自动保存。"""
from __future__ import annotations

from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget

from ui.pages.settings_page import SettingsPage
from ui.theme.theme_manager import ensure_theme


class SettingsDialog(QDialog):
    """独立设置窗口；实际设置在 SettingsPage 内自动保存。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        ensure_theme(self)
        self.setWindowTitle("设置")
        self.resize(760, 680)
        self.setMinimumSize(560, 520)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.page = SettingsPage(self)
        lay.addWidget(self.page)

        # ---- 兼容代理 ----
        self._cb_delete_source = self.page.cb_delete_source
        self._edit_bandizip_path = self.page.edit_bandizip_path
        self._edit_winrar_path = self.page.edit_winrar_path
        self._edit_custom_path = self.page.edit_custom_path
        self._spin_workers = self.page.spin_workers
        self._spin_scan_depth = self.page.spin_scan_depth
        self._spin_min_size_mb = self.page.spin_min_size

    def _on_save(self) -> bool:
        """兼容旧接口：总是写回当前控件值。"""
        ok = self.page.flush()
        if ok:
            self.accept()
        return bool(ok)
