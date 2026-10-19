"""Fluent 下拉框 (ComboBox)：隐藏原生三角箭头，自绘 16px chevron。

原生 QComboBox 箭头由 style 绘制，QSS 无法可靠着色/缩放（审计问题 #4），
因此在右侧叠一个透明命中区的自绘 chevron，点击仍然落在 drop-down 区域。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import QComboBox, QWidget

from ui.theme.theme_manager import get_current_theme

_ARROW_W = 16
_ARROW_H = 16
_RIGHT_OFFSET = 10


class _Chevron(QWidget):
    """只负责画一个向下的小箭头。"""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFixedSize(_ARROW_W, _ARROW_H)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event: QPaintEvent):
        theme = get_current_theme()
        parent = self.parentWidget()
        enabled = parent.isEnabled() if parent else True
        hovered = parent.underMouse() if parent else False

        if not enabled:
            color = QColor(theme.text3)
        else:
            color = QColor(theme.accent if hovered else theme.text2)

        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = p.pen()
        pen.setColor(color)
        pen.setWidthF(1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        cx = self.width() / 2.0
        cy = self.height() / 2.0
        half_w = 3.5
        half_h = 2.0
        p.drawLine(int(cx - half_w), int(cy - half_h), int(cx), int(cy + half_h))
        p.drawLine(int(cx), int(cy + half_h), int(cx + half_w), int(cy - half_h))
        p.end()

    def enterEvent(self, event):  # noqa: N802
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self.update()
        super().leaveEvent(event)


class ComboBox(QComboBox):
    """统一 32px 高、右侧自绘箭头的下拉框。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumWidth(180)
        self._arrow = _Chevron(self)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        x = self.width() - _ARROW_W - _RIGHT_OFFSET
        y = (self.height() - _ARROW_H) // 2
        self._arrow.move(x, y)

    def enterEvent(self, event):  # noqa: N802
        self._arrow.update()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self._arrow.update()
        super().leaveEvent(event)


def combo_with_items(items: list[tuple[str, object]], parent: QWidget | None = None) -> ComboBox:
    """构造下拉框：items 为 (显示文本, userData) 列表。"""
    box = ComboBox(parent)
    for text, data in items:
        box.addItem(text, data)
    return box
