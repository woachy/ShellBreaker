"""任务页底部 36px 统计条。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from ui.theme.theme_manager import get_current_theme
from ui.theme.tokens import STATUS_BAR_HEIGHT
from ui.widgets.toggle_switch import ToggleSwitch


class _Dot(QLabel):
    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(6, 6)

    def set_color(self, color: str) -> None:
        self.setStyleSheet(f"background: {color}; border-radius: 3px;")


class StatusBar(QFrame):
    """底部统计条：总任务 / 成功 / 失败 / 处理中 / 已跳过 + 回收站开关。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("StatusBar")
        self.setFixedHeight(STATUS_BAR_HEIGHT)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(24, 0, 24, 0)
        lay.setSpacing(14)

        # 兼容属性：MainWindow._lbl_stats 指向这里
        self.lbl_stats = QLabel("共 0 个任务", self)
        self.lbl_stats.setObjectName("StatusText")
        lay.addWidget(self.lbl_stats, 0, Qt.AlignmentFlag.AlignVCenter)

        self._dots: dict[str, _Dot] = {}
        self._labels: dict[str, QLabel] = {}
        for key, text in (
            ("success", "成功 0"),
            ("failed", "失败 0"),
            ("running", "处理中 0"),
            ("skipped", "已跳过 0"),
        ):
            dot = _Dot(self)
            label = QLabel(text, self)
            label.setObjectName("StatusText")
            self._dots[key] = dot
            self._labels[key] = label
            lay.addWidget(dot, 0, Qt.AlignmentFlag.AlignVCenter)
            lay.addWidget(label, 0, Qt.AlignmentFlag.AlignVCenter)
            lay.addSpacing(2)

        lay.addStretch(1)

        self.cb_delete_source = ToggleSwitch("", self)
        self.lbl_delete_source = QLabel("解压后移至回收站", self)
        self.lbl_delete_source.setObjectName("StatusText")
        lay.addWidget(self.cb_delete_source, 0, Qt.AlignmentFlag.AlignVCenter)
        lay.addSpacing(8)
        lay.addWidget(self.lbl_delete_source, 0, Qt.AlignmentFlag.AlignVCenter)

        self._apply_colors()

    # ---- 主题色 ----
    def _apply_colors(self) -> None:
        theme = get_current_theme()
        self._dots["success"].set_color(theme.success)
        self._dots["failed"].set_color(theme.danger)
        self._dots["running"].set_color(theme.accent)
        self._dots["skipped"].set_color(theme.warning)

    def showEvent(self, event):  # noqa: N802
        self._apply_colors()
        super().showEvent(event)

    # ---- 数据 ----
    def set_counts(
        self,
        total: int,
        success: int = 0,
        failed: int = 0,
        running: int = 0,
        skipped: int = 0,
    ) -> None:
        self.lbl_stats.setText(f"共 {total} 个任务")
        self._labels["success"].setText(f"成功 {success}")
        self._labels["failed"].setText(f"失败 {failed}")
        self._labels["running"].setText(f"处理中 {running}")
        self._labels["skipped"].setText(f"已跳过 {skipped}")
        self._apply_colors()
