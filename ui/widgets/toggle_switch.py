"""Fluent 风格切换开关 (ToggleSwitch)。

硬约束：必须继承 QCheckBox，保留 setChecked() / isChecked() / toggled 信号，
以便业务逻辑与测试无缝兼容（测试会 setChecked 后断言配置已写盘）。
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import QCheckBox, QWidget

from ui.theme.theme_manager import get_current_theme

_TRACK_W = 40.0
_TRACK_H = 20.0
_THUMB = 14.0
_THUMB_MARGIN = 3.0


class ToggleSwitch(QCheckBox):
    """40x20 Fluent 开关；整行可点，键盘 Space 可切换。"""

    def __init__(self, text: str = "", parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(20)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ---- 尺寸 ----
    def sizeHint(self) -> QSize:
        text_w = self.fontMetrics().horizontalAdvance(self.text()) if self.text() else 0
        gap = 8 if self.text() else 0
        return QSize(int(_TRACK_W) + gap + text_w, int(_TRACK_H))

    def _text_offset(self) -> float:
        return _TRACK_W + 8.0 if self.text() else 0.0

    def hitButton(self, pos) -> bool:  # noqa: N802 - Qt 命名
        return self.rect().contains(pos)

    # ---- 绘制 ----
    def paintEvent(self, event: QPaintEvent):
        theme = get_current_theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        checked = self.isChecked()
        enabled = self.isEnabled()
        hovered = self.underMouse()

        track = QRectF(0.0, (self.height() - _TRACK_H) / 2.0, _TRACK_W, _TRACK_H)
        radius = _TRACK_H / 2.0

        if not enabled:
            track_color = QColor(theme.row_hover)
            border_color = QColor(theme.divider)
            thumb_color = QColor(theme.text3)
        elif checked:
            track_color = QColor(theme.accent_hover if hovered else theme.accent)
            border_color = track_color
            thumb_color = QColor(theme.accent_text)
        else:
            track_color = QColor(theme.control_hover if hovered else theme.control)
            border_color = QColor(theme.text3 if hovered else theme.control_border)
            thumb_color = QColor(theme.text2)

        p.setPen(border_color)
        p.setBrush(track_color)
        p.drawRoundedRect(track, radius, radius)

        if self.hasFocus() and enabled:
            p.setPen(QColor(theme.accent))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(track.adjusted(-2, -2, 2, 2), radius + 2, radius + 2)

        thumb_x = (
            track.right() - _THUMB - _THUMB_MARGIN
            if checked
            else track.left() + _THUMB_MARGIN
        )
        thumb_y = track.top() + (_TRACK_H - _THUMB) / 2.0
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(thumb_color)
        p.drawEllipse(QRectF(thumb_x, thumb_y, _THUMB, _THUMB))

        if self.text():
            p.setFont(self.font())
            p.setPen(QColor(theme.text if enabled else theme.text3))
            p.drawText(
                QRectF(self._text_offset(), 0, self.width() - self._text_offset(), self.height()),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                self.text(),
            )
        p.end()


def toggle_with_label(text: str, parent: QWidget | None = None) -> ToggleSwitch:
    """便捷构造：带文字标签的开关。"""
    return ToggleSwitch(text, parent)
