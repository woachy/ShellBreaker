"""Fluent 数字步进框 (NumberBox)。

不用原生 UpDownArrows（点击目标小、样式不可控），改为：
- QSpinBox 关闭原生按钮（NoButtons）；
- 右侧两个 16x13 自绘步进按钮，键盘上下键仍然可用。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent
from PySide6.QtWidgets import QAbstractSpinBox, QPushButton, QSpinBox, QWidget

from ui.theme.theme_manager import get_current_theme

_STEP_W = 17
_STEP_H = 13
_RIGHT_PAD = 5


class _StepButton(QPushButton):
    """自绘上下箭头的小按钮。"""

    def __init__(self, direction: int, parent: QWidget):
        super().__init__(parent)
        self._direction = 1 if direction > 0 else -1
        self.setObjectName("SpinStep")
        self.setFixedSize(_STEP_W, _STEP_H)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAutoRepeat(True)
        self.setAutoRepeatDelay(400)
        self.setAutoRepeatInterval(60)

    def paintEvent(self, event: QPaintEvent):
        theme = get_current_theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        hovered = self.underMouse()
        if not self.isEnabled():
            color = QColor(theme.text3)
        else:
            color = QColor(theme.accent if hovered else theme.text2)

        cx = self.width() / 2.0
        cy = self.height() / 2.0
        half_w = 3.5
        half_h = 2.0
        pen = p.pen()
        pen.setColor(color)
        pen.setWidthF(1.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        if self._direction > 0:
            p.drawLine(int(cx - half_w), int(cy + half_h), int(cx), int(cy - half_h))
            p.drawLine(int(cx), int(cy - half_h), int(cx + half_w), int(cy + half_h))
        else:
            p.drawLine(int(cx - half_w), int(cy - half_h), int(cx), int(cy + half_h))
            p.drawLine(int(cx), int(cy + half_h), int(cx + half_w), int(cy - half_h))
        p.end()


class NumberBox(QSpinBox):
    """带后缀的标准数字输入框（124x32）。"""

    def __init__(
        self,
        minimum: int = 1,
        maximum: int = 100,
        value: int = 1,
        suffix: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setRange(minimum, maximum)
        self.setValue(value)
        if suffix:
            self.setSuffix(suffix)
        self.setFixedSize(124, 32)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.setStyleSheet("QSpinBox { padding-right: 24px; }")

        self._btn_up = _StepButton(1, self)
        self._btn_down = _StepButton(-1, self)
        self._btn_up.clicked.connect(lambda: self.stepBy(1))
        self._btn_down.clicked.connect(lambda: self.stepBy(-1))

    def resizeEvent(self, event):  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        x = self.width() - _STEP_W - _RIGHT_PAD
        self._btn_up.move(x, 3)
        self._btn_down.move(x, self.height() - _STEP_H - 3)
