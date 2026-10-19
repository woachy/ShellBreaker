"""解压任务页（任务看板）。

结构：页头（标题 / 副标题）→ 拖放卡（128）→ 任务卡（卡头 48 + 表格）→ 状态条（36）。
空状态挂在表格 viewport 上，绝不覆盖表头（审计问题 #1 的根治方案）。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ui.theme.fonts import display_font, text_font
from ui.theme.icons import ICON_DELETE, ICON_TASK, glyph_icon, glyph_pixmap
from ui.theme.theme_manager import get_current_theme
from ui.theme.tokens import TABLE_HEADER_HEIGHT, TABLE_ROW_HEIGHT
from ui.widgets.drop_card import DropCard
from ui.widgets.smooth_label import SmoothLabel
from ui.widgets.status_bar import StatusBar
from ui.widgets.status_pill import PrimaryTextDelegate, SecondaryTextDelegate, StatusPillDelegate

HEADERS = ["文件名", "处理状态", "使用引擎", "详细备注"]


class TasksPage(QWidget):
    """任务页容器。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("TasksPage")

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        content = QWidget(self)
        content_lay = QVBoxLayout(content)
        content_lay.setContentsMargins(24, 20, 24, 16)
        content_lay.setSpacing(16)

        # ---- 页头 ----
        header = QVBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(4)
        self.title_label = SmoothLabel("解压任务", self, get_current_theme().text)
        self.title_label.setObjectName("PageTitle")
        self.title_label.setFont(display_font(28))
        self.subtitle_label = QLabel("拖入文件或文件夹，自动识别伪装并逐层解压", self)
        self.subtitle_label.setObjectName("PageSubtitle")
        header.addWidget(self.title_label)
        header.addWidget(self.subtitle_label)
        content_lay.addLayout(header)

        # ---- 拖放卡 ----
        self.drop_card = DropCard(self)
        content_lay.addWidget(self.drop_card)

        # ---- 任务卡 ----
        self.task_card = QFrame(self)
        self.task_card.setObjectName("Card")
        card_lay = QVBoxLayout(self.task_card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        card_header = QWidget(self.task_card)
        card_header.setFixedHeight(48)
        head_lay = QHBoxLayout(card_header)
        head_lay.setContentsMargins(16, 0, 12, 0)
        head_lay.setSpacing(8)

        self.card_title = SmoothLabel("任务", card_header, get_current_theme().text)
        self.card_title.setObjectName("CardTitle")
        self.card_title.setFont(text_font(14, QFont.Weight.DemiBold))
        head_lay.addWidget(self.card_title, 0, Qt.AlignmentFlag.AlignVCenter)

        self.count_badge = QLabel("0", card_header)
        self.count_badge.setObjectName("CountBadge")
        self.count_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count_badge.setFixedHeight(18)
        self.count_badge.setMinimumWidth(22)
        head_lay.addWidget(self.count_badge, 0, Qt.AlignmentFlag.AlignVCenter)

        head_lay.addStretch(1)

        self.btn_clean_finished = QPushButton("清空已完成", card_header)
        self.btn_clean_finished.setObjectName("Subtle")
        self.btn_clean_finished.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_clean_finished.setFixedHeight(28)
        self.btn_clean_finished.setIconSize(QSize(16, 16))
        self.btn_clean_finished.setIcon(glyph_icon(ICON_DELETE, get_current_theme().text2, 16))
        head_lay.addWidget(self.btn_clean_finished, 0, Qt.AlignmentFlag.AlignVCenter)

        card_lay.addWidget(card_header)

        # ---- 表格 ----
        self.table = QTableWidget(0, 4, self.task_card)
        self.table.setObjectName("TaskTable")
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(TABLE_ROW_HEIGHT)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setFrameShape(QFrame.Shape.NoFrame)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # 拖放统一交给 MainWindow，避免表格吞掉 drop 事件造成重复提交
        self.table.setAcceptDrops(False)
        self.table.viewport().setAcceptDrops(False)

        header_view = self.table.horizontalHeader()
        header_view.setFixedHeight(TABLE_HEADER_HEIGHT)
        header_view.setHighlightSections(False)
        header_view.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header_view.resizeSection(1, 168)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header_view.resizeSection(2, 172)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)

        self.table.setItemDelegateForColumn(0, PrimaryTextDelegate(self.table))
        self.table.setItemDelegateForColumn(1, StatusPillDelegate(self.table))
        self.table.setItemDelegateForColumn(2, SecondaryTextDelegate(self.table))
        self.table.setItemDelegateForColumn(3, SecondaryTextDelegate(self.table))

        card_lay.addWidget(self.table, 1)
        content_lay.addWidget(self.task_card, 1)

        root.addWidget(content, 1)

        # ---- 状态条 ----
        self.status_bar = StatusBar(self)
        root.addWidget(self.status_bar)

        # ---- 空状态（挂在 viewport 上，不遮表头） ----
        self.empty_widget = QWidget(self.table.viewport())
        self.empty_widget.setObjectName("EmptyState")
        empty_lay = QVBoxLayout(self.empty_widget)
        empty_lay.setContentsMargins(24, 24, 24, 24)
        empty_lay.setSpacing(8)
        empty_lay.addStretch(1)

        self.empty_icon = QLabel(self.empty_widget)
        self.empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_lay.addWidget(self.empty_icon)

        self.empty_title = QLabel("还没有任务", self.empty_widget)
        self.empty_title.setObjectName("EmptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_lay.addWidget(self.empty_title)

        self.empty_desc = QLabel("拖入压缩包或文件夹，或点击右上角「选择文件」开始", self.empty_widget)
        self.empty_desc.setObjectName("EmptyDesc")
        self.empty_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_lay.addWidget(self.empty_desc)
        empty_lay.addStretch(1)

        self.table.viewport().installEventFilter(self)
        self._refresh_empty_icon()
        self.update_empty_state()

    # ---- 兼容属性 ----
    @property
    def cb_delete_source(self):
        return self.status_bar.cb_delete_source

    @property
    def lbl_stats(self):
        return self.status_bar.lbl_stats

    # ---- 空状态 ----
    def _refresh_empty_icon(self) -> None:
        theme = get_current_theme()
        pixmap = glyph_pixmap(ICON_TASK, theme.text3, 32, dpr=self.devicePixelRatioF() or 2.0)
        self.empty_icon.setPixmap(pixmap)

    def _resize_empty(self) -> None:
        self.empty_widget.setGeometry(self.table.viewport().rect())

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self.table.viewport() and event.type() in (
            QEvent.Type.Resize,
            QEvent.Type.Show,
        ):
            self._resize_empty()
        return super().eventFilter(obj, event)

    def update_empty_state(self) -> None:
        """按行数显示/隐藏空状态（不影响表头）。"""
        self._resize_empty()
        self.empty_widget.setVisible(self.table.rowCount() == 0)
        if self.empty_widget.isVisible():
            self.empty_widget.raise_()
        self.set_task_count(self.table.rowCount())

    def set_task_count(self, count: int) -> None:
        self.count_badge.setText(str(count))

    def showEvent(self, event):  # noqa: N802
        self._refresh_empty_icon()
        self._resize_empty()
        super().showEvent(event)
