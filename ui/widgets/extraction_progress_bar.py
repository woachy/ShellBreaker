"""解压进度与绿格折叠组件 (ExtractionProgressWidget)。

符合 V2 Fluent 设计体系（适配浅色/深色主题）：
- 左侧已完成层绿格区：
  - completed_count <= 0：不绘制、不占宽；
  - 1 <= completed_count <= max_green_cells：横向绘制等量绿色小方块 (10x14, r=2, gap=3)；
  - completed_count > max_green_cells：折叠收敛为「1 个绿格 + × N」加粗文本。
- 右侧当前层进度条区：
  - 圆角槽 (h=14, r=4) + 蓝色进度条 (#3B82F6)，支持不确定态往返滑块动画；
  - 右侧定宽 36px 右对齐紧凑文字（45% / 100% / 等待 / 中止 / 失败 / 跳过）。
"""
from __future__ import annotations

import math
from typing import Optional

from PySide6.QtCore import QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPaintEvent
from PySide6.QtWidgets import QSizePolicy, QWidget

from ui.theme.theme_manager import get_current_theme

# 几何常量（逻辑像素）
_PAD_LEFT = 12.0
_PAD_RIGHT = 8.0
_CELL_W = 10.0
_CELL_H = 14.0
_CELL_RADIUS = 2.0
_CELL_GAP = 3.0
_COLLAPSED_TEXT_GAP = 4.0
_SECTION_GAP = 6.0
_TRACK_H = 14.0
_TRACK_RADIUS = 4.0
_TEXT_GAP = 6.0
_PERCENT_TEXT_W = 36.0
_MIN_TRACK_W = 24.0

# 语义色常量（浅色 / 深色）
_GREEN_CELL = "#10B981"
_GREEN_TEXT_LIGHT = "#059669"
_GREEN_TEXT_DARK = "#34D399"
_BLUE_FILL = "#3B82F6"
_SUCCESS_FILL = "#10B981"
_FAIL_FILL_LIGHT = "#EF4444"
_FAIL_FILL_DARK = "#F87171"
_MUTED_FILL_LIGHT = "#9CA3AF"
_MUTED_FILL_DARK = "#6B7280"
_TRACK_LIGHT = "#E5E7EB"
_TRACK_DARK = "#374151"

_ANIM_INTERVAL_MS = 30
_ANIM_STEP = 0.032


def _is_dark_theme() -> bool:
    theme = get_current_theme()
    return "dark" in (theme.name or "").lower()


class ExtractionProgressWidget(QWidget):
    """任务表「解压进度」列自绘控件：已完成层绿格（支持折叠）+ 当前层进度条。"""

    def __init__(self, max_green_cells: int = 5, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._max_green_cells: int = max(1, int(max_green_cells))
        self._current_percent: int = 0
        self._completed_count: int = 0
        self._status_text: str = ""
        self._terminal_state: Optional[str] = None
        self._has_started: bool = False

        # 不确定态往返滑块相位 [0.0, 2.0)
        self._anim_phase: float = 0.0
        self._indeterminate_timer = QTimer(self)
        self._indeterminate_timer.setInterval(_ANIM_INTERVAL_MS)
        self._indeterminate_timer.timeout.connect(self._on_anim_tick)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setMinimumWidth(150)
        self.setMinimumHeight(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # ---- 只读属性 ----
    @property
    def current_percent(self) -> int:
        return self._current_percent

    @property
    def completed_count(self) -> int:
        return self._completed_count

    @property
    def max_green_cells(self) -> int:
        return self._max_green_cells

    @property
    def status_text(self) -> str:
        return self._status_text

    @property
    def terminal_state(self) -> Optional[str]:
        return self._terminal_state

    @property
    def is_collapsed(self) -> bool:
        return self._completed_count > self._max_green_cells

    @property
    def is_indeterminate(self) -> bool:
        return self._current_percent < 0 and self._terminal_state is None

    # ---- 尺寸建议 ----
    def sizeHint(self) -> QSize:
        return QSize(196, 24)

    def minimumSizeHint(self) -> QSize:
        return QSize(150, 24)

    # ---- 公共状态控制 ----
    def update_state(
        self,
        current_percent: int,
        completed_count: int,
        status_text: str = "",
    ) -> None:
        """更新当前层百分比、已完成层绿格数与悬浮提示文本。"""
        raw_pct = int(current_percent)
        self._current_percent = min(100, raw_pct) if raw_pct >= 0 else raw_pct
        self._completed_count = max(0, int(completed_count))
        self._status_text = str(status_text or "")
        self._terminal_state = None
        self._has_started = (
            self._current_percent != 0
            or self._completed_count > 0
            or (bool(self._status_text) and "排队" not in self._status_text and "等待" not in self._status_text)
        )
        self.setToolTip(self._status_text)

        if self._current_percent < 0:
            if not self._indeterminate_timer.isActive():
                self._indeterminate_timer.start()
        else:
            if self._indeterminate_timer.isActive():
                self._indeterminate_timer.stop()

        self.update()

    def set_max_green_cells(self, max_cells: int) -> None:
        """设置绿格折叠阈值，超出该值自动收敛为「1 绿格 × N」。"""
        clamped = max(1, int(max_cells))
        if clamped != self._max_green_cells:
            self._max_green_cells = clamped
            self.update()

    def set_terminal_state(self, status: str) -> None:
        """进入终态（成功 / 失败 / 已中止 / 已跳过）：停止动画并呈现终态视觉。"""
        text = (status or "").strip()
        self._terminal_state = text
        self.stop_animation()

        if text.startswith("成功"):
            self._current_percent = 100
            self._has_started = True
        elif self._current_percent < 0:
            # 不确定模式下遭遇失败/中止：冻结在静态进度态，不再滚动
            self._current_percent = 0

        if text and not self._status_text:
            self.setToolTip(text)

        self.update()

    def stop_animation(self) -> None:
        """停止不确定态定时器（释放 CPU 占用）。"""
        if self._indeterminate_timer.isActive():
            self._indeterminate_timer.stop()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.stop_animation()
        super().closeEvent(event)

    # ---- 内部动画 ----
    def _on_anim_tick(self) -> None:
        self._anim_phase = (self._anim_phase + _ANIM_STEP) % 2.0
        self.update()

    # ---- 文本与颜色辅助 ----
    def _right_label_text(self) -> str:
        term = self._terminal_state
        if term:
            if term.startswith("成功"):
                return "100%"
            if "中止" in term:
                return "中止"
            if term.startswith("失败"):
                return "失败"
            if "跳过" in term:
                return "跳过"
        if self._current_percent < 0:
            if "等待" in self._status_text or "排队" in self._status_text:
                return "等待"
            return "提取中"
        if not self._has_started and self._current_percent == 0 and self._completed_count == 0:
            return "等待"
        pct = max(0, min(100, self._current_percent))
        return f"{pct}%"

    def _bar_fill_color(self, dark: bool) -> QColor:
        term = self._terminal_state
        if term:
            if term.startswith("成功"):
                return QColor(_SUCCESS_FILL)
            if term.startswith("失败"):
                return QColor(_FAIL_FILL_DARK if dark else _FAIL_FILL_LIGHT)
            return QColor(_MUTED_FILL_DARK if dark else _MUTED_FILL_LIGHT)
        return QColor(_BLUE_FILL)

    def _right_text_color(self, dark: bool) -> QColor:
        theme = get_current_theme()
        term = self._terminal_state
        if term:
            if term.startswith("成功"):
                return QColor(_GREEN_TEXT_DARK if dark else _GREEN_TEXT_LIGHT)
            if term.startswith("失败") or "中止" in term:
                return QColor(theme.danger)
            if "跳过" in term:
                return QColor(theme.warning)
        return QColor(theme.text2)

    # ---- 自绘 ----
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

        dark = _is_dark_theme()
        w = float(self.width())
        h = float(self.height())
        cy = h / 2.0
        cell_y = cy - _CELL_H / 2.0
        track_y = cy - _TRACK_H / 2.0

        x = _PAD_LEFT

        # 1. 左侧已完成层绿格区
        if self._completed_count > 0:
            green_color = QColor(_GREEN_CELL)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(green_color)

            if self._completed_count <= self._max_green_cells:
                for i in range(self._completed_count):
                    p.drawRoundedRect(
                        QRectF(x, cell_y, _CELL_W, _CELL_H),
                        _CELL_RADIUS,
                        _CELL_RADIUS,
                    )
                    x += _CELL_W
                    if i < self._completed_count - 1:
                        x += _CELL_GAP
            else:
                # 折叠态：1 个绿格 + 加粗 "× N"
                p.drawRoundedRect(
                    QRectF(x, cell_y, _CELL_W, _CELL_H),
                    _CELL_RADIUS,
                    _CELL_RADIUS,
                )
                x += _CELL_W + _COLLAPSED_TEXT_GAP

                count_font = QFont(self.font())
                count_font.setPixelSize(12)
                count_font.setBold(True)
                p.setFont(count_font)
                count_text = f"× {self._completed_count}"
                fm = QFontMetricsF(count_font)
                text_w = fm.horizontalAdvance(count_text)
                p.setPen(QColor(_GREEN_TEXT_DARK if dark else _GREEN_TEXT_LIGHT))
                p.drawText(
                    QRectF(x, 0.0, text_w + 2.0, h),
                    int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                    count_text,
                )
                x += text_w

            x += _SECTION_GAP

        # 2. 右侧紧凑百分比/状态文字区域（定宽右对齐，防止进度条宽度抖动）
        label_x = max(x + _MIN_TRACK_W + _TEXT_GAP, w - _PAD_RIGHT - _PERCENT_TEXT_W)
        track_right = label_x - _TEXT_GAP
        track_w = max(_MIN_TRACK_W, track_right - x)

        # 3. 当前层进度槽与填充条
        track_rect = QRectF(x, track_y, track_w, _TRACK_H)
        track_path = QPainterPath()
        track_path.addRoundedRect(track_rect, _TRACK_RADIUS, _TRACK_RADIUS)

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(_TRACK_DARK if dark else _TRACK_LIGHT))
        p.drawPath(track_path)

        fill_color = self._bar_fill_color(dark)
        p.save()
        p.setClipPath(track_path)
        p.setBrush(fill_color)

        if self.is_indeterminate:
            # 往返正弦缓动滑块
            slider_w = max(18.0, track_w * 0.32)
            travel = max(0.0, track_w - slider_w)
            t = 0.5 - 0.5 * math.cos(self._anim_phase * math.pi)
            slider_x = x + travel * t
            p.drawRoundedRect(
                QRectF(slider_x, track_y, slider_w, _TRACK_H),
                _TRACK_RADIUS,
                _TRACK_RADIUS,
            )
        else:
            pct = max(0, min(100, self._current_percent))
            if pct > 0:
                fill_w = track_w * (pct / 100.0)
                if fill_w < 4.0:
                    fill_w = 4.0
                p.drawRoundedRect(
                    QRectF(x, track_y, min(track_w, fill_w), _TRACK_H),
                    _TRACK_RADIUS,
                    _TRACK_RADIUS,
                )
        p.restore()

        # 4. 绘制右侧百分比/状态文字
        pct_font = QFont(self.font())
        pct_font.setPixelSize(11)
        pct_font.setBold(bool(self._terminal_state and self._terminal_state.startswith("成功")))
        p.setFont(pct_font)
        p.setPen(self._right_text_color(dark))
        p.drawText(
            QRectF(label_x, 0.0, _PERCENT_TEXT_W, h),
            int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
            self._right_label_text(),
        )

        p.end()
