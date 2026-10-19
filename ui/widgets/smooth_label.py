"""灰度抗锯齿标签。

背景：Windows 上 Qt 6 走 DirectWrite，`QFont.PreferAntialias` / `NoSubpixelAntialias`
都不再生效（实测与默认策略像素完全一致），ClearType 的彩色子像素描边会让大号粗体
出现明显的「毛边」。唯一可靠的办法是把文字画进 ARGB 画布——带 alpha 的目标设
备不会启用子像素抗锯齿，于是得到干净的灰度边缘。

本控件只用于大号粗体标题（页面标题 / 对话框标题 / 拖放卡标题），
正文仍走系统 ClearType，保持小字号下的清晰度。
"""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPalette
from PySide6.QtWidgets import QLabel, QWidget


class SmoothLabel(QLabel):
    """把文本渲染到 ARGB 画布上的 QLabel（灰度抗锯齿，无 ClearType 彩边）。"""

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        color: QColor | str | None = None,
    ):
        super().__init__(text, parent)
        self._text_color = QColor(color) if color is not None else None
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

    def set_text_color(self, color: QColor | str) -> None:
        self._text_color = QColor(color)
        self.update()

    def text_color(self) -> QColor:
        if self._text_color is not None:
            return QColor(self._text_color)
        return self.palette().color(QPalette.ColorRole.WindowText)

    # QLabel 的接口保持不变：setText / text / setFont / alignment 都照旧可用
    def paintEvent(self, event) -> None:  # noqa: N802
        dpr = self.devicePixelRatioF() or 1.0
        width = max(1, int(round(self.width() * dpr)))
        height = max(1, int(round(self.height() * dpr)))

        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(dpr)
        image.fill(Qt.GlobalColor.transparent)

        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setFont(self.font())
        painter.setPen(self.text_color())
        flags = int(self.alignment())
        if self.wordWrap():
            flags |= int(Qt.TextFlag.TextWordWrap)
        painter.drawText(
            QRectF(0.0, 0.0, float(self.width()), float(self.height())), flags, self.text()
        )
        painter.end()

        out = QPainter(self)
        out.drawImage(QPointF(0.0, 0.0), image)
        out.end()
