"""设置分组：分组标题 + 说明 + 1px 圆角卡片容器。"""
from __future__ import annotations

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from ui.theme.fonts import text_font
from ui.theme.theme_manager import get_current_theme
from ui.widgets.smooth_label import SmoothLabel


class SettingsGroup(QWidget):
    """一组设置行，行之间自动插入 1px 分隔线。"""

    def __init__(self, title: str, desc: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("SettingsGroup")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(8)

        # 分组标题：粗体，走灰度抗锯齿（避免 ClearType 彩边）
        self.title_label = SmoothLabel(title, self, get_current_theme().text)
        self.title_label.setObjectName("SectionTitle")
        self.title_label.setFont(text_font(14, QFont.Weight.DemiBold))
        outer.addWidget(self.title_label)

        self.desc_label = QLabel(desc, self)
        self.desc_label.setObjectName("SectionDesc")
        self.desc_label.setWordWrap(True)
        self.desc_label.setVisible(bool(desc))
        outer.addWidget(self.desc_label)

        self.card = QFrame(self)
        self.card.setObjectName("GroupCard")
        self._card_lay = QVBoxLayout(self.card)
        self._card_lay.setContentsMargins(0, 0, 0, 0)
        self._card_lay.setSpacing(0)
        outer.addWidget(self.card)

    def add_row(self, row: QWidget):
        if self._card_lay.count() > 0:
            divider = QFrame(self.card)
            divider.setObjectName("RowDivider")
            divider.setFixedHeight(1)
            self._card_lay.addWidget(divider)
        self._card_lay.addWidget(row)
