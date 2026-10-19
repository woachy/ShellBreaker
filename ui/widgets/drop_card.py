"""任务页顶部拖放卡片（128px）。

整窗拖放由 MainWindow 统一处理，本控件只负责视觉反馈（高亮 / 提示文案），
不注册自己的 drop handler，避免一次拖入被提交两次。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.theme.fonts import text_font
from ui.theme.icons import ICON_FOLDER, glyph_pixmap
from ui.theme.theme_manager import get_current_theme, repolish
from ui.widgets.smooth_label import SmoothLabel

DEFAULT_TITLE = "拖入压缩包或文件夹"
DEFAULT_SUBTITLE = "支持 ZIP / RAR / 7Z / TAR 与伪装后缀，自动递归解压"
DRAG_TITLE = "松开放置文件"
DRAG_SUBTITLE = "将立即开始识别与解压"


class DropCard(QFrame):
    """拖放目标卡片。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("DropCard")
        self.setFixedHeight(128)
        self.setProperty("drag", "false")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 16, 20, 16)
        lay.setSpacing(16)

        # ---- 图标底板 56x56 ----
        self.icon_box = QLabel(self)
        self.icon_box.setObjectName("DropIconBox")
        self.icon_box.setFixedSize(56, 56)
        self.icon_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._refresh_icon()
        lay.addWidget(self.icon_box, 0, Qt.AlignmentFlag.AlignVCenter)

        # ---- 文案 ----
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(6)

        self.title_label = SmoothLabel(DEFAULT_TITLE, self, get_current_theme().text)
        self.title_label.setObjectName("DropTitle")
        self.title_label.setFont(text_font(16, QFont.Weight.DemiBold))
        text_col.addWidget(self.title_label)

        self.subtitle_label = QLabel(DEFAULT_SUBTITLE, self)
        self.subtitle_label.setObjectName("DropSubtitle")
        self.subtitle_label.setWordWrap(False)
        text_col.addWidget(self.subtitle_label)
        lay.addLayout(text_col, 1)

        # ---- 动作按钮 ----
        self.btn_select_files = QPushButton("选择文件", self)
        self.btn_select_files.setObjectName("Primary")
        self.btn_select_files.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_select_files.setMinimumWidth(96)

        self.btn_select_dir = QPushButton("选择文件夹", self)
        self.btn_select_dir.setObjectName("Secondary")
        self.btn_select_dir.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_select_dir.setMinimumWidth(104)
        self.btn_select_folder = self.btn_select_dir  # 兼容别名

        lay.addWidget(self.btn_select_files, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addWidget(self.btn_select_dir, 0, Qt.AlignmentFlag.AlignVCenter)

        # 向后兼容旧字段名
        self._hint_label = self.subtitle_label

    # ---- 图标 ----
    def _refresh_icon(self) -> None:
        theme = get_current_theme()
        pixmap = glyph_pixmap(ICON_FOLDER, theme.accent, 26, dpr=self.devicePixelRatioF() or 2.0)
        self.icon_box.setPixmap(pixmap)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._refresh_icon()

    # ---- 拖拽高亮 ----
    def set_drag_active(self, active: bool) -> None:
        self.setProperty("drag", "true" if active else "false")
        self.title_label.setText(DRAG_TITLE if active else DEFAULT_TITLE)
        self.subtitle_label.setText(DRAG_SUBTITLE if active else DEFAULT_SUBTITLE)
        repolish(self)

    # 兼容旧方法名
    def _set_drag_active(self, active: bool) -> None:
        self.set_drag_active(active)

    # ---- 提示 ----
    def set_received_hint(self, text: str) -> None:
        self.subtitle_label.setText(text)

    def reset_hint(self) -> None:
        self.subtitle_label.setText(DEFAULT_SUBTITLE)
