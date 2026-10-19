"""居中内容列：宽度自适应并限制最大宽度（修复设置页宽度塌缩）。"""
from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QWidget

from ui.theme.tokens import CONTENT_MAX_WIDTH, PAGE_MARGIN


class CenteredColumn(QWidget):
    """把 `body` 居中，宽度取 min(可用宽度, max_width)。

    父级（QScrollArea 的 content widget）尺寸变化时重算，不依赖内部布局的
    sizeHint，因此不会出现“只占子控件最小宽度”的塌缩。
    """

    def __init__(
        self,
        max_width: int = CONTENT_MAX_WIDTH,
        margins: tuple[int, int, int, int] = (PAGE_MARGIN, 20, PAGE_MARGIN, 24),
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._max_width = max_width
        self._outer = QHBoxLayout(self)
        self._outer.setContentsMargins(*margins)
        self._outer.setSpacing(0)

        self.body = QWidget(self)
        self.body.setObjectName("ColumnBody")
        self._outer.addStretch(1)
        self._outer.addWidget(self.body, 0)
        self._outer.addStretch(1)

    def resizeEvent(self, event):  # noqa: N802
        super().resizeEvent(event)
        m = self._outer.contentsMargins()
        avail = self.width() - m.left() - m.right()
        self.body.setFixedWidth(max(320, min(self._max_width, avail)))
