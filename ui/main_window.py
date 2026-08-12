"""
懒人解压😋 — 主窗口 UI（阶段 4.5 重构 + 主题美化）
QTableWidget 任务看板 + 拖拽放置区 + 全局设置入口。
"""
import os
import logging
import uuid
import urllib.parse
from typing import Callable

from PySide6.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QPushButton,
    QMessageBox,
    QInputDialog,
    QSizePolicy,
    QHeaderView,
    QAbstractItemView,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QDragEnterEvent, QDropEvent

from core.task_queue import TaskQueue
from core.sandbox import SandboxManager

logger = logging.getLogger("ShellBreaker.MainWindow")


# 任务状态 → 展示颜色（与 style.qss 语义色保持一致）
STATUS_COLORS = {
    "成功": "#16A34A",
    "失败": "#DC2626",
    "正在提取": "#2563EB",
    "验证密码": "#B45309",
    "排队中": "#94A3B8",
    "已跳过": "#B45309",
}
_DEFAULT_STATUS_COLOR = "#334155"


class DropZone(QGroupBox):
    """拖拽放置区 — 支持文件拖入，执行路径清洗（铁律一）。"""

    def __init__(self, parent=None):
        super().__init__("📦 拖拽放置区", parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(110)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setProperty("drag_active", False)

        layout = QVBoxLayout(self)
        self._hint_label = QLabel("将文件或文件夹拖拽到此处")
        self._hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._hint_label)

        self._on_path_ready: Callable[[str], None] | None = None

    def set_path_callback(self, callback: Callable[[str], None]):
        self._on_path_ready = callback

    def _set_drag_active(self, active: bool):
        """切换拖拽高亮状态（供 QSS 动态属性选择器使用）。"""
        self.setProperty("drag_active", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event: QDragEnterEvent):
        mime_data = event.mimeData()
        if mime_data.hasUrls():
            event.acceptProposedAction()
            self._hint_label.setText("✨ 松开放置文件")
            self._set_drag_active(True)
            logger.debug("拖拽进入放置区")

    def dragLeaveEvent(self, event):
        self._hint_label.setText("将文件或文件夹拖拽到此处")
        self._set_drag_active(False)

    def dropEvent(self, event: QDropEvent):
        mime_data = event.mimeData()
        if not mime_data.hasUrls():
            self._set_drag_active(False)
            return
        urls = mime_data.urls()
        if not urls:
            self._set_drag_active(False)
            return

        raw_url = urls[0].toString()
        logger.info(f"原始拖拽URL: {raw_url}")

        if raw_url.startswith("file:///"):
            path = raw_url[8:]
        elif raw_url.startswith("file://"):
            path = raw_url[7:]
        else:
            path = raw_url
        logger.debug(f"剔除前缀后: {path}")

        path = urllib.parse.unquote(path)
        logger.debug(f"unquote 解码后: {path}")

        clean_path = os.path.abspath(path)
        logger.info(f"清洗完成 — 绝对路径: {clean_path}")

        if self._on_path_ready:
            self._on_path_ready(clean_path)

        self._hint_label.setText(f"已接收: {clean_path}")
        self._set_drag_active(False)
        event.acceptProposedAction()


class MainWindow(QMainWindow):
    """懒人解压😋 主窗口。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("懒人解压😋")
        self.resize(820, 560)

        # ---- 核心层 ----
        self._task_queue = TaskQueue()
        self._sandbox_mgr = SandboxManager()

        # ---- 信号 ----
        self._task_queue.task_status_changed.connect(self._on_task_status_changed)
        self._task_queue.task_progress.connect(self._on_task_progress)
        self._task_queue.password_needed.connect(self._on_password_needed)
        self._task_queue.password_wrong.connect(self._on_password_wrong)

        # 中心部件
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setSpacing(10)
        root.setContentsMargins(14, 14, 14, 14)

        # ---- 拖拽放置区 ----
        self.drop_zone = DropZone()
        self.drop_zone.set_path_callback(self._on_drop_path_ready)
        root.addWidget(self.drop_zone)

        # ---- 任务看板 (QTableWidget) ----
        task_group = QGroupBox("📋 任务看板")
        task_layout = QVBoxLayout(task_group)
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["文件名", "处理状态", "使用引擎", "详细备注"])
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(32)
        task_layout.addWidget(self._table)
        root.addWidget(task_group, stretch=1)

        # ---- 底部按钮区 ----
        btn_layout = QHBoxLayout()

        self.btn_clear_cache = QPushButton("🗑️ 一键清空缓存")
        self.btn_clear_cache.setMinimumHeight(34)
        self.btn_clear_cache.setToolTip("清除所有临时沙箱文件")
        self.btn_clear_cache.clicked.connect(self._on_clear_cache_clicked)

        self.btn_password_mgr = QPushButton("🔑 密码管理")
        self.btn_password_mgr.setMinimumHeight(34)
        self.btn_password_mgr.setToolTip("管理解压密码库")
        self.btn_password_mgr.clicked.connect(self._on_password_mgr_clicked)

        self.btn_settings = QPushButton("⚙️ 全局设置")
        self.btn_settings.setMinimumHeight(34)
        self.btn_settings.setToolTip("解压引擎、输出路径、并发数设置")
        self.btn_settings.clicked.connect(self._on_settings_clicked)

        btn_layout.addWidget(self.btn_clear_cache)
        btn_layout.addWidget(self.btn_password_mgr)
        btn_layout.addWidget(self.btn_settings)
        root.addLayout(btn_layout)

        # 行索引映射：{task_id: row_index}
        self._task_rows: dict[str, int] = {}

        # 启动调度器
        self._task_queue.start()

    def closeEvent(self, event):
        self._task_queue.shutdown(wait=False)
        super().closeEvent(event)

    # ---------- 拖拽 ----------

    def _on_drop_path_ready(self, clean_path: str):
        """拖拽入口安检：严格分流文件与文件夹。"""
        if os.path.isfile(clean_path):
            task_id = str(uuid.uuid4())[:8]
            filename = os.path.basename(clean_path)
            # 大小预检查
            if self._task_queue._is_file_too_small(clean_path):
                self._add_task_row(task_id, filename)
                self._set_cell(self._task_rows[task_id], 1, "已跳过(文件过小)")
                logger.info(f"文件过小，跳过: {filename} ({os.path.getsize(clean_path)} bytes)")
                return
            self._add_task_row(task_id, filename)
            self._set_cell(self._task_rows[task_id], 1, "排队中")
            self._task_queue.submit_existing(task_id, clean_path)
        elif os.path.isdir(clean_path):
            # 文件夹：转交递归扫描器，绝不允许直接传给 shutil.copy
            self._handle_directory_drop(clean_path)
        else:
            QMessageBox.warning(self, "路径错误", f"路径不存在:\n{clean_path}")

    def _handle_directory_drop(self, dir_path: str):
        """将文件夹转交递归扫描器，并在状态栏提示。"""
        logger.info(f"拖入文件夹，启动递归扫描: {dir_path}")
        self.drop_zone._hint_label.setText(f"🔍 正在扫描文件夹: {dir_path}")
        self._task_queue.scan_directory(dir_path)
        self.drop_zone._hint_label.setText("将文件或文件夹拖拽到此处")

    # ---------- QTableWidget 任务看板 ----------

    def _add_task_row(self, task_id: str, filename: str):
        row = self._table.rowCount()
        self._table.insertRow(row)
        self._table.setItem(row, 0, QTableWidgetItem(filename))
        for col in range(4):
            item = self._table.item(row, col) or QTableWidgetItem("")
            item.setData(Qt.ItemDataRole.UserRole, task_id)
            if not self._table.item(row, col):
                self._table.setItem(row, col, item)
        self._task_rows[task_id] = row

    def _on_task_status_changed(self, task_id: str, filename: str, status: str, engine: str, remark: str):
        """信号驱动：原地更新表格行，绝不允许插入重复行（修复"分身"Bug）。"""
        row = self._task_rows.get(task_id)
        if row is None:
            if status == "排队中":
                # 递归扫描首次发现：插入新行（唯一合法的插入时机）
                self._add_task_row(task_id, filename)
                row = self._task_rows[task_id]
            else:
                # 非"排队中"状态但找不到行 → 异常，记录并忽略
                logger.warning(f"收到状态更新但 task_id 不在表格中: {task_id} status={status}")
                return

        if filename:
            item = self._table.item(row, 0)
            if item:
                item.setText(filename)
        self._set_cell(row, 1, status)
        self._set_cell(row, 2, engine)
        self._set_cell(row, 3, remark)
        self._table.scrollToBottom()

    def _set_cell(self, row: int, col: int, text: str):
        item = self._table.item(row, col)
        if item is None:
            item = QTableWidgetItem(text)
            self._table.setItem(row, col, item)
        else:
            item.setText(text)

        # 状态列语义着色；引擎列使用次要文字色
        if col == 1:
            color_key = next((k for k in STATUS_COLORS if text.startswith(k)), None)
            hex_color = STATUS_COLORS.get(color_key, _DEFAULT_STATUS_COLOR) if color_key else _DEFAULT_STATUS_COLOR
            item.setForeground(QBrush(QColor(hex_color)))
            font = item.font()
            font.setBold(True)
            item.setFont(font)
        elif col == 2:
            item.setForeground(QBrush(QColor("#64748B")))

    def _on_task_progress(self, task_id: str, status_text: str):
        """保持兼容：将进度写入备注列。"""
        row = self._task_rows.get(task_id)
        if row is not None:
            self._set_cell(row, 3, status_text)

    # ---------- 密码自学习 ----------

    def _on_password_needed(self, task_id: str, archive_name: str):
        logger.info(f"[{task_id}] 弹出密码输入框: {archive_name}")
        password, ok = QInputDialog.getText(
            self,
            "需要密码",
            f"加密包: {archive_name}\n密码库已全部尝试失败，请手动输入密码：\n（取消则跳过此任务）",
        )
        if ok and password.strip():
            self._task_queue.supply_password(task_id, password.strip())
        else:
            self._task_queue.supply_password(task_id, None)

    def _on_password_wrong(self, task_id: str):
        """密码错误 → 弹出错误提示，然后再次请求输入（允许无限重试）。"""
        QMessageBox.warning(
            self,
            "密码错误",
            "密码错误，请重新输入！\n（点击取消则跳过此任务）",
        )
        # 触发密码输入弹窗（与 password_needed 复用同一流程）
        password, ok = QInputDialog.getText(
            self,
            "重新输入密码",
            "请再次输入密码：\n（取消则跳过此任务）",
        )
        if ok and password.strip():
            self._task_queue.supply_password(task_id, password.strip())
        else:
            self._task_queue.supply_password(task_id, None)

    # ---------- 按钮事件 ----------

    def _on_clear_cache_clicked(self):
        logger.info("用户点击「一键清空缓存」")
        reply = QMessageBox.question(
            self, "确认清空", "确定要清除所有沙箱临时文件吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        count = self._sandbox_mgr.clean_all()
        QMessageBox.information(self, "完成", f"已清空 {count} 个临时沙箱目录")

    def _on_password_mgr_clicked(self):
        logger.info("用户点击「密码管理」")
        from ui.password_dialog import PasswordDialog
        PasswordDialog(self).exec()

    def _on_settings_clicked(self):
        logger.info("用户点击「全局设置」")
        from ui.settings_dialog import SettingsDialog
        dialog = SettingsDialog(self)
        if dialog.exec():
            # 用户点击了保存 → 重载调度器配置
            self._task_queue.reload_config()
