"""路径选择控件：只读输入框 + 浏览 + 清除。"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFileDialog, QHBoxLayout, QLineEdit, QPushButton, QWidget

from ui.theme.icons import ICON_FOLDER_OPEN, glyph_icon
from ui.theme.theme_manager import ensure_theme, get_current_theme


class PathPicker(QWidget):
    """只读路径框 + [浏览…] + [清除]。"""

    path_changed = Signal(str)

    FILE = "file"
    DIR = "dir"

    def __init__(
        self,
        dialog_title: str,
        mode: str = FILE,
        placeholder: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        ensure_theme(self)
        self._dialog_title = dialog_title
        self._mode = mode

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        self.line_edit = QLineEdit(self)
        self.line_edit.setReadOnly(True)
        self.line_edit.setPlaceholderText(placeholder)
        self.line_edit.setMinimumWidth(150)
        self.line_edit.setMaximumWidth(280)
        self.line_edit.setProperty("mono", "true")
        lay.addWidget(self.line_edit, 1)

        self.btn_browse = QPushButton("浏览…", self)
        self.btn_browse.setObjectName("Secondary")
        self.btn_browse.setCursor(Qt.CursorShape.PointingHandCursor)
        lay.addWidget(self.btn_browse)

        self.btn_clear = QPushButton("清除", self)
        self.btn_clear.setObjectName("Subtle")
        self.btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        lay.addWidget(self.btn_clear)

        self.btn_browse.clicked.connect(self._on_browse)
        self.btn_clear.clicked.connect(self._on_clear)

        self._refresh_icon()

    # ---- 值 ----
    def path(self) -> str:
        return self.line_edit.text().strip()

    def set_path(self, path: str):
        self.line_edit.setText(path or "")
        self.line_edit.setToolTip(path or "")
        self.line_edit.setCursorPosition(0)

    def clear(self):
        self.set_path("")

    def _on_browse(self):
        if self._mode == self.DIR:
            path = QFileDialog.getExistingDirectory(self, self._dialog_title, self.path() or "")
        else:
            path, _ = QFileDialog.getOpenFileName(
                self,
                self._dialog_title,
                self.path() or "",
                "可执行文件 (*.exe);;所有文件 (*)",
            )
        if not path:
            return
        path = os.path.abspath(path)
        self.set_path(path)
        self.path_changed.emit(path)

    def _on_clear(self):
        if not self.path():
            return
        self.clear()
        self.path_changed.emit("")

    def _refresh_icon(self):
        theme = get_current_theme()
        try:
            self.btn_browse.setIcon(glyph_icon(ICON_FOLDER_OPEN, theme.text2, 16))
        except Exception:
            pass

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._refresh_icon()
