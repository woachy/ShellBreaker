"""
密码管理 QDialog — 独立窗口，展示密码列表（含使用次数），支持增删清空、
剪贴板批量导入与文本文件批量导入（智能清洗与预览确认）。
严格遵循 MVC：UI 层仅负责渲染与用户交互，数据操作委托给 data.PasswordManager。
"""
import logging
from pathlib import Path

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
    QFileDialog,
    QAbstractItemView,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication

from data.password_manager import (
    PasswordManager,
    parse_passwords_text,
    read_text_safe,
)

logger = logging.getLogger("ShellBreaker.PasswordDialog")


class PasswordDialog(QDialog):
    """密码管理独立窗口。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("🔑 密码管理")
        self.resize(560, 460)
        self.setMinimumSize(460, 320)

        # 数据层（单例）
        self._mgr = PasswordManager()

        # ---------- UI 搭建 ----------
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # 表格（密码明文、使用次数、标签）
        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["密码（明文）", "使用次数", "标签"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(self._table)

        # 底部操作栏拆为两行，防挤压
        btn_box = QVBoxLayout()
        btn_box.setSpacing(6)

        # 第一行：➕ 手动添加、📋 剪贴板导入、📄 文件导入
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        self._btn_add = QPushButton("➕ 手动添加")
        self._btn_add.setMinimumHeight(32)
        self._btn_add.clicked.connect(self._on_add)

        self._btn_import_clip = QPushButton("📋 剪贴板导入")
        self._btn_import_clip.setMinimumHeight(32)
        self._btn_import_clip.clicked.connect(self._on_import_clipboard)

        self._btn_import_file = QPushButton("📄 文件导入")
        self._btn_import_file.setMinimumHeight(32)
        self._btn_import_file.clicked.connect(self._on_import_file)

        row1.addWidget(self._btn_add)
        row1.addWidget(self._btn_import_clip)
        row1.addWidget(self._btn_import_file)
        btn_box.addLayout(row1)

        # 第二行：🗑️ 删除选中、🧹 清空密码库
        row2 = QHBoxLayout()
        row2.setSpacing(8)
        self._btn_delete = QPushButton("🗑️ 删除选中")
        self._btn_delete.setMinimumHeight(32)
        self._btn_delete.clicked.connect(self._on_delete)

        self._btn_clear = QPushButton("🧹 清空密码库")
        self._btn_clear.setMinimumHeight(32)
        self._btn_clear.clicked.connect(self._on_clear)

        row2.addWidget(self._btn_delete)
        row2.addWidget(self._btn_clear)
        btn_box.addLayout(row2)

        layout.addLayout(btn_box)

        # 加载数据
        self._refresh_table()

    # ---------- 表格刷新 ----------

    def _refresh_table(self):
        """从数据层重新加载全部记录并刷新表格。"""
        data = self._mgr.get_all()
        self._table.setUpdatesEnabled(False)
        try:
            self._table.setRowCount(len(data))
            for row, record in enumerate(data):
                pwd = record.get("password", "")
                count_val = record.get("count", 0)
                tag_val = record.get("tag", "")

                pw_item = QTableWidgetItem(pwd)
                pw_item.setData(Qt.ItemDataRole.UserRole, pwd)  # 存储具体密码字符串

                count_item = QTableWidgetItem(str(count_val))
                count_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

                tag_item = QTableWidgetItem(tag_val)

                self._table.setItem(row, 0, pw_item)
                self._table.setItem(row, 1, count_item)
                self._table.setItem(row, 2, tag_item)
        finally:
            self._table.setUpdatesEnabled(True)
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
        logger.info(f"手动添加密码完成，重排索引={idx}")

    def _on_delete(self):
        """删除当前选中的密码行（根据密码值精确删除，解决行索引漂移）。"""
        current_row = self._table.currentRow()
        if current_row < 0:
            QMessageBox.information(self, "提示", "请先选中一条密码记录")
            return

        item = self._table.item(current_row, 0)
        if not item:
            return
        password = item.data(Qt.ItemDataRole.UserRole) or item.text()

        # 确认
        reply = QMessageBox.question(
            self,
            "确认删除",
            f"确定要删除选中的密码记录吗？此操作不可撤销。\n密码: {password}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        ok = self._mgr.delete_by_password(password)
        if ok:
            self._refresh_table()
            logger.info(f"已通过 UI 删除密码: '{password}'")
        else:
            QMessageBox.warning(self, "删除失败", f"未能删除密码: '{password}'")

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

    def _on_import_clipboard(self):
        """从系统剪贴板批量导入密码（智能识别与预览确认）。"""
        clipboard = QGuiApplication.clipboard()
        text = clipboard.text() if clipboard else ""
        if not text or not text.strip():
            QMessageBox.information(self, "提示", "剪贴板为空，未发现可导入内容")
            return

        candidates = parse_passwords_text(text)
        if not candidates:
            QMessageBox.information(self, "提示", "剪贴板文本中未识别到有效密码候选词")
            return

        preview_lines = [f"{i+1}. {p}" for i, p in enumerate(candidates[:5])]
        if len(candidates) > 5:
            preview_lines.append(f"... 等共 {len(candidates)} 条")
        preview_text = "\n".join(preview_lines)

        reply = QMessageBox.question(
            self,
            "确认批量导入",
            f"从剪贴板识别到 {len(candidates)} 条密码候选，前 5 条预览：\n\n{preview_text}\n\n是否确认导入并自动去重？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        added, duplicates = self._mgr.batch_add(candidates, tag="[剪贴板导入]")
        self._refresh_table()
        QMessageBox.information(
            self,
            "导入完成",
            f"剪贴板批量导入完成：\n成功新增: {added} 条\n忽略重复: {duplicates} 条",
        )

    def _on_import_file(self):
        """从文本文件批量导入密码（多编码自适应解码与预览确认）。"""
        file_path, _ = QFileDialog.getOpenFileName(
            self, "选择密码文本文件", "", "文本文件 (*.txt *.log);;所有文件 (*.*)"
        )
        if not file_path:
            return

        try:
            content = read_text_safe(file_path)
        except Exception as e:
            QMessageBox.warning(self, "读取失败", f"无法读取文件内容:\n{e}")
            return

        if not content.strip():
            QMessageBox.information(self, "提示", "文件为空，未发现可导入内容")
            return

        candidates = parse_passwords_text(content)
        if not candidates:
            QMessageBox.information(self, "提示", "文件中未识别到有效密码候选词")
            return

        preview_lines = [f"{i+1}. {p}" for i, p in enumerate(candidates[:5])]
        if len(candidates) > 5:
            preview_lines.append(f"... 等共 {len(candidates)} 条")
        preview_text = "\n".join(preview_lines)

        reply = QMessageBox.question(
            self,
            "确认批量导入",
            f"从文件识别到 {len(candidates)} 条密码候选，前 5 条预览：\n\n{preview_text}\n\n是否确认导入并自动去重？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        added, duplicates = self._mgr.batch_add(candidates, tag="[文件导入]")
        self._refresh_table()
        QMessageBox.information(
            self,
            "导入完成",
            f"文件批量导入完成：\n成功新增: {added} 条\n忽略重复: {duplicates} 条",
        )