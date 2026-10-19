"""密码库页（V2）：3 列表格 + 工具栏 + 空状态。"""
from __future__ import annotations

import os

from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from data.password_manager import PasswordManager, parse_passwords_text, read_text_safe
from ui.theme.fonts import display_font, text_font
from ui.theme.icons import (
    ICON_ADD,
    ICON_CLEAN,
    ICON_DELETE,
    ICON_IMPORT,
    ICON_KEY,
    ICON_PASTE,
    glyph_icon,
    glyph_pixmap,
)
from ui.theme.theme_manager import ensure_theme, get_current_theme
from ui.theme.tokens import MONO_FAMILIES
from ui.widgets.centered_column import CenteredColumn
from ui.widgets.content_dialog import ImportPreviewDialog
from ui.widgets.smooth_label import SmoothLabel
from ui.widgets.status_pill import PrimaryTextDelegate, SecondaryTextDelegate

PASSWORD_HEADERS = ["密码（明文）", "使用次数", "标签"]
TAG_MANUAL = "[手动入库]"
TAG_CLIP = "[剪贴板导入]"
TAG_FILE = "[文件导入]"


class PasswordsPage(QWidget):
    """密码库页面。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        ensure_theme(self)
        self.setObjectName("PasswordsPage")

        self._mgr = PasswordManager()

        self._build_ui()
        self._refresh_table()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.centered = CenteredColumn(parent=self)
        self.column = self.centered.body
        self.column.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        root.addWidget(self.centered)

        col = QVBoxLayout(self.column)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(16)

        # ---- 页头 ----
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 4)
        header.setSpacing(12)
        title_col = QVBoxLayout()
        title_col.setContentsMargins(0, 0, 0, 0)
        title_col.setSpacing(4)
        self.title_label = SmoothLabel("密码库", self.column, get_current_theme().text)
        self.title_label.setObjectName("PageTitle")
        self.title_label.setFont(display_font(28))
        title_col.addWidget(self.title_label)
        self.subtitle_label = QLabel("共 0 条密码 · 按使用频率排序", self.column)
        self.subtitle_label.setObjectName("PageSubtitle")
        title_col.addWidget(self.subtitle_label)
        header.addLayout(title_col, 1)

        self._btn_add = QPushButton("添加密码", self.column)
        self._btn_add.setObjectName("Primary")
        self._btn_add.setCursor(Qt.CursorShape.PointingHandCursor)
        header.addWidget(self._btn_add, 0, Qt.AlignmentFlag.AlignTop)
        col.addLayout(header)

        # ---- 表格卡 ----
        self.table_card = QFrame(self.column)
        self.table_card.setObjectName("Card")
        card = QVBoxLayout(self.table_card)
        card.setContentsMargins(0, 0, 0, 0)
        card.setSpacing(0)

        head = QWidget(self.table_card)
        head.setFixedHeight(48)
        head_lay = QHBoxLayout(head)
        head_lay.setContentsMargins(16, 0, 16, 0)
        head_lay.setSpacing(8)

        self.card_title = SmoothLabel("全部密码", head, get_current_theme().text)
        self.card_title.setObjectName("CardTitle")
        self.card_title.setFont(text_font(14, QFont.Weight.DemiBold))
        head_lay.addWidget(self.card_title)

        self.count_badge = QLabel("0", head)
        self.count_badge.setObjectName("CountBadge")
        self.count_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.count_badge.setFixedHeight(18)
        self.count_badge.setMinimumWidth(22)
        head_lay.addWidget(self.count_badge)
        head_lay.addStretch(1)

        self._btn_import_clip = QPushButton("从剪贴板导入", head)
        self._btn_import_clip.setObjectName("Subtle")
        self._btn_import_clip.setCursor(Qt.CursorShape.PointingHandCursor)
        head_lay.addWidget(self._btn_import_clip)

        self._btn_import_file = QPushButton("从文件导入", head)
        self._btn_import_file.setObjectName("Subtle")
        self._btn_import_file.setCursor(Qt.CursorShape.PointingHandCursor)
        head_lay.addWidget(self._btn_import_file)

        self._btn_delete = QPushButton("删除", head)
        self._btn_delete.setObjectName("Subtle")
        self._btn_delete.setCursor(Qt.CursorShape.PointingHandCursor)
        head_lay.addWidget(self._btn_delete)

        self._btn_clear = QPushButton("清空", head)
        self._btn_clear.setObjectName("DangerSubtle")
        self._btn_clear.setCursor(Qt.CursorShape.PointingHandCursor)
        head_lay.addWidget(self._btn_clear)
        card.addWidget(head)

        self._table = QTableWidget(0, 3, self.table_card)
        self._table.setHorizontalHeaderLabels(PASSWORD_HEADERS)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(44)
        self._table.horizontalHeader().setFixedHeight(36)
        self._table.horizontalHeader().setDefaultAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self._table.horizontalHeader().setHighlightSections(False)
        self._table.setShowGrid(False)
        self._table.setAlternatingRowColors(False)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setWordWrap(False)
        self._table.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._table.horizontalHeader().setStretchLastSection(False)
        self._table.setColumnWidth(0, 420)
        self._table.setColumnWidth(1, 110)
        self._table.setColumnWidth(2, 200)
        self._table.horizontalHeader().setSectionResizeMode(0, self._table.horizontalHeader().ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, self._table.horizontalHeader().ResizeMode.Fixed)
        self._table.horizontalHeader().setSectionResizeMode(2, self._table.horizontalHeader().ResizeMode.Fixed)
        self._table.setItemDelegateForColumn(0, PrimaryTextDelegate(self._table))
        self._table.setItemDelegateForColumn(1, SecondaryTextDelegate(self._table))
        self._table.setItemDelegateForColumn(2, SecondaryTextDelegate(self._table))
        card.addWidget(self._table, 1)

        col.addWidget(self.table_card, 1)

        # ---- 空状态（挂在 viewport 上，绝不遮挡表头） ----
        self.empty_widget = QWidget(self._table.viewport())
        self.empty_widget.setObjectName("EmptyState")
        empty_lay = QVBoxLayout(self.empty_widget)
        empty_lay.setContentsMargins(0, 0, 0, 0)
        empty_lay.setSpacing(6)
        empty_lay.addStretch(1)
        self.empty_icon = QLabel(self.empty_widget)
        self.empty_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_lay.addWidget(self.empty_icon)
        self.empty_title = QLabel("还没有保存的密码", self.empty_widget)
        self.empty_title.setObjectName("EmptyTitle")
        self.empty_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_lay.addWidget(self.empty_title)
        self.empty_desc = QLabel("从剪贴板或文件导入，或在解压时自动学习保存", self.empty_widget)
        self.empty_desc.setObjectName("EmptyDesc")
        self.empty_desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        empty_lay.addWidget(self.empty_desc)
        empty_lay.addStretch(1)
        self.empty_widget.hide()

        self._table.viewport().installEventFilter(self)

        # ---- 信号 ----
        self._btn_add.clicked.connect(self._on_add)
        self._btn_import_clip.clicked.connect(self._on_import_clipboard)
        self._btn_import_file.clicked.connect(self._on_import_file)
        self._btn_delete.clicked.connect(self._on_delete)
        self._btn_clear.clicked.connect(self._on_clear)

        self._refresh_icons()

    def _refresh_icons(self):
        theme = get_current_theme()
        self._btn_add.setIcon(glyph_icon(ICON_ADD, theme.accent_text, 16))
        self._btn_import_clip.setIcon(glyph_icon(ICON_PASTE, theme.text2, 16))
        self._btn_import_file.setIcon(glyph_icon(ICON_IMPORT, theme.text2, 16))
        self._btn_delete.setIcon(glyph_icon(ICON_DELETE, theme.text2, 16))
        self._btn_clear.setIcon(glyph_icon(ICON_CLEAN, theme.danger, 16))
        self._refresh_empty_icon()

    def _refresh_empty_icon(self):
        theme = get_current_theme()
        self.empty_icon.setPixmap(glyph_pixmap(ICON_KEY, theme.text3, 32, 2.0))

    # ---------------------------------------------------------------- 表格
    def _refresh_table(self):
        """重新从管理器拉取数据并填充表格（按使用频率排序）。"""
        items = self._mgr.get_all()
        self._table.blockSignals(True)
        self._table.setRowCount(0)
        mono = QFont()
        mono.setFamilies(MONO_FAMILIES)
        mono.setPixelSize(13)
        for row, item in enumerate(items):
            self._table.insertRow(row)
            pwd = str(item.get("password", ""))
            count_text = str(item.get("count", 0))
            tag = str(item.get("tag", ""))
            for col, text in enumerate((pwd, count_text, tag)):
                cell = QTableWidgetItem(text)
                cell.setData(Qt.ItemDataRole.UserRole, pwd)
                cell.setToolTip(text)
                if col == 0:
                    cell.setFont(mono)
                self._table.setItem(row, col, cell)
        self._table.blockSignals(False)

        total = len(items)
        self.count_badge.setText(str(total))
        self.subtitle_label.setText(f"共 {total} 条密码 · 按使用频率排序")
        self.update_empty_state()

    # 兼容别名
    refresh_table = _refresh_table

    def update_empty_state(self):
        has_rows = self._table.rowCount() > 0
        self.empty_widget.setVisible(not has_rows)
        if not has_rows:
            self._resize_empty()

    def _resize_empty(self):
        self.empty_widget.setGeometry(self._table.viewport().rect())

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self._table.viewport() and event.type() in (QEvent.Type.Resize, QEvent.Type.Show):
            self._resize_empty()
        return super().eventFilter(obj, event)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._refresh_empty_icon()
        self._resize_empty()

    def _selected_password(self) -> str | None:
        row = self._table.currentRow()
        if row < 0:
            rows = self._table.selectionModel().selectedRows() if self._table.selectionModel() else []
            row = rows[0].row() if rows else -1
        if row < 0:
            return None
        item = self._table.item(row, 0)
        return item.text() if item else None

    # ---------------------------------------------------------------- 动作
    def _on_add(self):
        text, ok = QInputDialog.getText(self, "添加密码", "请输入要保存的密码：")
        if not ok:
            return
        pwd = (text or "").strip()
        if not pwd:
            QMessageBox.information(self, "提示", "密码不能为空。")
            return
        self._mgr.add(pwd, TAG_MANUAL)
        self._refresh_table()

    def _on_delete(self):
        pwd = self._selected_password()
        if not pwd:
            QMessageBox.information(self, "提示", "请先选择要删除的密码。")
            return
        reply = QMessageBox.question(
            self,
            "删除密码",
            "确定要删除选中的密码吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._mgr.delete_by_password(pwd)
        self._refresh_table()

    def _on_clear(self):
        reply = QMessageBox.warning(
            self,
            "清空密码库",
            "确定要清空所有已保存的密码吗？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._mgr.clear_all()
        self._refresh_table()

    def _preview_and_import(self, candidates: list[str], source_name: str, tag: str):
        if not candidates:
            QMessageBox.information(self, "提示", "没有解析到可用密码。")
            return
        dlg = ImportPreviewDialog(candidates, source_name, self)
        if dlg.exec() != ImportPreviewDialog.DialogCode.Accepted:
            return
        selected = dlg.result_passwords()
        if not selected:
            return
        added, duplicates = self._mgr.batch_add(selected, tag)
        QMessageBox.information(self, "导入完成", f"新增 {added} 条，忽略重复 {duplicates} 条。")
        self._refresh_table()

    def _on_import_clipboard(self):
        raw = QApplication.clipboard().text() or ""
        candidates = parse_passwords_text(raw)
        self._preview_and_import(candidates, "剪贴板", TAG_CLIP)

    def _on_import_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择密码文本文件", "", "文本文件 (*.txt *.log *.csv);;所有文件 (*)"
        )
        if not path:
            return
        raw = read_text_safe(path)
        candidates = parse_passwords_text(raw)
        self._preview_and_import(candidates, os.path.basename(path), TAG_FILE)

