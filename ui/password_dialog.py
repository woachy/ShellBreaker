"""密码库对话框（兼容层）：内嵌 PasswordsPage。"""
from __future__ import annotations

from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget

from ui.pages.passwords_page import PasswordsPage
from ui.theme.theme_manager import ensure_theme


class PasswordDialog(QDialog):
    """独立密码库窗口。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        ensure_theme(self)
        self.setWindowTitle("密码库")
        self.resize(760, 560)
        self.setMinimumSize(520, 400)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        self.page = PasswordsPage(self)
        lay.addWidget(self.page)

        # ---- 兼容代理 ----
        self._table = self.page._table
        self._btn_add = self.page._btn_add
        self._btn_import_clip = self.page._btn_import_clip
        self._btn_import_file = self.page._btn_import_file
        self._btn_delete = self.page._btn_delete
        self._btn_clear = self.page._btn_clear

    def _refresh_table(self):
        self.page._refresh_table()
