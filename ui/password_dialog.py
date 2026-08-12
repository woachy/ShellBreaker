"""
密码管理 QDialog — 独立窗口，展示密码列表，支持增删清空。
严格遵循 MVC：UI 层仅负责渲染与用户交互，数据操作委托给 data.PasswordManager。
"""
import logging

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QTableWidget,
    QTableWidgetItem,
    QPushButton,
    QHeaderView,
    QMessageBox,
    QInputDialog,
    QAbstractItemView,
)
from PySide6.QtCore import Qt

from data.password_manager import PasswordManager

logger = logging.getLogger("ShellBreaker.PasswordDialog")


class PasswordDialog(QDialog):
    """密码管理独立窗口。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🔑 密码管理")
        self.resize(560, 420)
        self.setMinimumSize(420, 300)

        # 数据层
        self._mgr = PasswordManager()

        # ---------- UI 搭建 ----------
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # 表格
        self._table = QTableWidget(0, 2)
        self._table.setHorizontalHeaderLabels(["密码（明文）", "标签"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self._table)

        # 按钮区
        btn_layout = QHBoxLayout()
        self._btn_add = QPushButton("➕ 手动添加密码")
        self._btn_add.setMinimumHeight(34)
        self._btn_add.clicked.connect(self._on_add)

        self._btn_delete = QPushButton("🗑️ 删除选中密码")
        self._btn_delete.setMinimumHeight(34)
        self._btn_delete.clicked.connect(self._on_delete)

        self._btn_clear = QPushButton("🧹 清空密码库")
        self._btn_clear.setMinimumHeight(34)
        self._btn_clear.clicked.connect(self._on_clear)

        btn_layout.addWidget(self._btn_add)
        btn_layout.addWidget(self._btn_delete)
        btn_layout.addWidget(self._btn_clear)
        layout.addLayout(btn_layout)

        # 加载数据
        self._refresh_table()

    # ---------- 表格刷新 ----------

    def _refresh_table(self):
        """从数据层重新加载全部记录并刷新表格。"""
        data = self._mgr.get_all()
        self._table.setRowCount(len(data))
        for row, record in enumerate(data):
            pw_item = QTableWidgetItem(record.get("password", ""))
            pw_item.setData(Qt.ItemDataRole.UserRole, row)  # 存储索引
            tag_item = QTableWidgetItem(record.get("tag", ""))
            self._table.setItem(row, 0, pw_item)
            self._table.setItem(row, 1, tag_item)
        logger.debug(f"表格已刷新，共 {len(data)} 条")

    # ---------- 按钮事件 ----------

    def _on_add(self):
        """手动添加密码。"""
        password, ok = QInputDialog.getText(
            self, "添加密码", "请输入密码（明文）："
        )
        if not ok or not password.strip():
            return
        password = password.strip()
        tag = "[手动入库]"
        idx = self._mgr.add(password, tag)
        self._refresh_table()
        logger.info(f"手动添加密码完成，索引={idx}")

    def _on_delete(self):
        """删除当前选中的密码行。"""
        current_row = self._table.currentRow()
        if current_row < 0:
            QMessageBox.information(self, "提示", "请先选中一条密码记录")
            return

        # 确认
        reply = QMessageBox.question(
            self,
            "确认删除",
            "确定要删除选中的密码记录吗？此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        item = self._table.item(current_row, 0)
        index = item.data(Qt.ItemDataRole.UserRole)
        removed = self._mgr.delete(index)
        if removed is not None:
            self._refresh_table()
            logger.info(f"已通过 UI 删除密码，标签: {removed.get('tag')}")

    def _on_clear(self):
        """清空全部密码库。"""
        count = self._mgr.count
        if count == 0:
            QMessageBox.information(self, "提示", "密码库已为空，无需清空")
            return

        reply = QMessageBox.warning(
            self,
            "确认清空",
            f"确定要清空全部 {count} 条密码记录吗？此操作不可撤销！",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        deleted = self._mgr.clear_all()
        self._refresh_table()
        QMessageBox.information(self, "完成", f"已清空 {deleted} 条密码记录")
        logger.info(f"已通过 UI 清空密码库，删除 {deleted} 条")