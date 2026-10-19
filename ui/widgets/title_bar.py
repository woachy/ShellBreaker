"""40px 自绘标题栏。

- 左侧：应用图标 + 应用名（12px 次要文字色）；
- 右侧：最小化 / 最大化-还原 / 关闭三个自绘按钮；
- 按住标题栏调用 startSystemMove()，保留 Windows 贴边分屏；
- 双击切换最大化。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from ui.theme.icons import app_icon_pixmap
from ui.theme.theme_manager import get_current_theme
from ui.theme.tokens import TITLE_BAR_HEIGHT

_BTN_W = 46
_BTN_H = TITLE_BAR_HEIGHT


class CaptionButton(QPushButton):
    """自绘窗口按钮：minimize / maximize / restore / close。"""

    def __init__(self, kind: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._kind = kind
        self.setObjectName("CaptionClose" if kind == "close" else "CaptionButton")
        self.setFixedSize(_BTN_W, _BTN_H)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        names = {
            "minimize": "最小化",
            "maximize": "最大化",
            "restore": "还原",
            "close": "关闭",
        }
        self.setToolTip(names.get(kind, ""))
        self.setAccessibleName(names.get(kind, ""))

    def set_kind(self, kind: str) -> None:
        if self._kind != kind:
            self._kind = kind
            self.update()

    def paintEvent(self, event: QPaintEvent):
        theme = get_current_theme()
        is_close = self._kind == "close"
        hovered = self.underMouse()
        pressed = self.isDown()

        # 背景
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if hovered or pressed:
            if is_close:
                bg = QColor("#C42B1C" if not pressed else "#A92716")
            else:
                bg = QColor(theme.control_pressed if pressed else theme.caption_hover)
            p.fillRect(self.rect(), bg)

        # 前景
        if is_close and (hovered or pressed):
            color = QColor("#FFFFFF")
        else:
            color = QColor(theme.text2)

        pen = QPen(color)
        pen.setWidthF(1.0)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)

        cx = self.width() / 2.0
        cy = self.height() / 2.0
        s = 5.0  # 半边长（10px 图形）

        if self._kind == "minimize":
            p.drawLine(int(cx - s), int(cy), int(cx + s), int(cy))
        elif self._kind == "maximize":
            p.drawRect(int(cx - s), int(cy - s), int(s * 2), int(s * 2))
        elif self._kind == "restore":
            p.drawRect(int(cx - s), int(cy - s + 2), int(s * 2 - 2), int(s * 2 - 2))
            p.drawLine(int(cx - s + 2), int(cy - s), int(cx + s), int(cy - s))
            p.drawLine(int(cx + s), int(cy - s), int(cx + s), int(cy + s - 2))
        elif self._kind == "close":
            p.drawLine(int(cx - s), int(cy - s), int(cx + s), int(cy + s))
            p.drawLine(int(cx + s), int(cy - s), int(cx - s), int(cy + s))
        p.end()


class TitleBar(QWidget):
    """窗口顶部 40px 标题栏。"""

    def __init__(self, window: QWidget, title: str = "便捷解压", parent: QWidget | None = None):
        super().__init__(parent or window)
        self._window = window
        self.setObjectName("TitleBar")
        self.setFixedHeight(TITLE_BAR_HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 0, 0)
        lay.setSpacing(8)

        self.icon_label = QLabel(self)
        self.icon_label.setObjectName("AppIcon")
        self.icon_label.setFixedSize(16, 16)
        pixmap = app_icon_pixmap(16)
        if not pixmap.isNull():
            self.icon_label.setPixmap(pixmap)
        lay.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignVCenter)

        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("AppTitle")
        lay.addWidget(self.title_label, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addStretch(1)

        self.btn_min = CaptionButton("minimize", self)
        self.btn_max = CaptionButton("maximize", self)
        self.btn_close = CaptionButton("close", self)
        for btn in (self.btn_min, self.btn_max, self.btn_close):
            btn.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
            lay.addWidget(btn)

        self.btn_min.clicked.connect(self._window.showMinimized)
        self.btn_max.clicked.connect(self.toggle_maximize)
        self.btn_close.clicked.connect(self._window.close)

        self.sync_state()

    # ---- 窗口状态 ----
    def toggle_maximize(self) -> None:
        if self._window.isMaximized():
            self._window.showNormal()
        else:
            self._window.showMaximized()
        self.sync_state()

    def sync_state(self) -> None:
        maximized = self._window.isMaximized()
        self.btn_max.set_kind("restore" if maximized else "maximize")
        self.btn_max.setToolTip("还原" if maximized else "最大化")

    def set_title(self, title: str) -> None:
        self.title_label.setText(title)

    # ---- 拖动 ----
    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self._window.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximize()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)
