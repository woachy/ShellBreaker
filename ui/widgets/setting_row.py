"""设置行：左侧标题/说明，右侧控件。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget


class SettingRow(QWidget):
    """一行设置项（固定 64px，无边框，由 SettingsGroup 负责分隔线）。"""

    def __init__(self, title: str, desc: str = "", control: QWidget | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("SettingRow")
        self.setFixedHeight(64 if desc else 56)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 16, 0)
        lay.setSpacing(16)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)

        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("RowTitle")
        text_col.addWidget(self.title_label)

        self.desc_label = QLabel(desc, self)
        self.desc_label.setObjectName("RowDesc")
        self.desc_label.setWordWrap(False)
        self.desc_label.setVisible(bool(desc))
        text_col.addWidget(self.desc_label)

        self.error_label = QLabel("", self)
        self.error_label.setObjectName("ErrorText")
        self.error_label.setVisible(False)
        text_col.addWidget(self.error_label)

        lay.addLayout(text_col, 1)

        self.control = control
        if control is not None:
            lay.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)

    # ---- 状态 ----
    def set_error(self, text: str):
        """行内错误提示（红色小字），空串表示清除。"""
        self.error_label.setText(text or "")
        self.error_label.setVisible(bool(text))
        self.desc_label.setVisible(bool(self.desc_label.text()) and not text)

    def set_muted(self, muted: bool):
        """把该行置为“当前不可用”的弱化状态。"""
        self.desc_label.setProperty("muted", "true" if muted else "false")
        self.title_label.setProperty("muted", "true" if muted else "false")
        for label in (self.title_label, self.desc_label):
            label.style().unpolish(label)
            label.style().polish(label)
        if self.control is not None:
            self.control.setEnabled(not muted)

    def has_error(self) -> bool:
        return self.error_label.isVisible()
