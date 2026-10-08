"""
懒人解压😋 — 主窗口 UI（UI 现代化重构 + 功能交互增强）
QTableWidget 任务看板 + 拖拽/点击双模放置区 + 状态统计栏 + 快捷菜单与路径定位。
"""
import os
import logging
import uuid
import urllib.parse
import subprocess
from typing import Callable, Optional

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
    QCheckBox,
    QMessageBox,
    QInputDialog,
    QSizePolicy,
    QHeaderView,
    QAbstractItemView,
    QFileDialog,
    QMenu,
)
from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import (
    QBrush,
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QDragMoveEvent,
    QDragLeaveEvent,
    QGuiApplication,
)

from core.task_queue import TaskQueue
from core.sandbox import SandboxManager, PROTECTED_ENDPOINT_EXTS, is_secondary_volume
from data.config_manager import ConfigManager

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
    """拖拽放置区 — 支持文件拖入与手动按钮选择，严格执行路径清洗（铁律一）。"""

    def __init__(self, parent=None):
        super().__init__("📦 拖拽放置区", parent)
        self.setObjectName("DropZone")
        self.setAcceptDrops(True)
        self.setMinimumHeight(115)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setProperty("drag_active", False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)

        self._hint_label = QLabel("将文件或文件夹拖拽到此处，或点击下方按钮直接选择")
        self._hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._hint_label)

        # 内部操作按钮：[📂 选择文件] 和 [📁 选择目录]
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(12)
        btn_layout.addStretch(1)

        self.btn_select_files = QPushButton("📂 选择文件")
        self.btn_select_files.setObjectName("DropZoneBtnFile")
        self.btn_select_files.setCursor(Qt.CursorShape.PointingHandCursor)

        self.btn_select_dir = QPushButton("📁 选择目录")
        self.btn_select_dir.setObjectName("DropZoneBtnDir")
        self.btn_select_dir.setCursor(Qt.CursorShape.PointingHandCursor)

        btn_layout.addWidget(self.btn_select_files)
        btn_layout.addWidget(self.btn_select_dir)
        btn_layout.addStretch(1)

        layout.addLayout(btn_layout)

        self._on_path_ready: Optional[Callable[[str], None]] = None
        self._on_paths_ready: Optional[Callable[[list[str]], None]] = None

    def set_path_callback(self, callback: Callable[[str], None]):
        self._on_path_ready = callback

    def set_paths_callback(self, callback: Callable[[list[str]], None]):
        self._on_paths_ready = callback

    def _set_drag_active(self, active: bool):
        """切换拖拽高亮状态（供 QSS 动态属性选择器使用）。"""
        self.setProperty("drag_active", active)
        self.style().unpolish(self)
        self.style().polish(self)

    @staticmethod
    def clean_path_from_url(raw_url: str) -> str:
        """路径清洗（铁律一：剔除 file:/// 前缀并 unquote 解码，转为绝对路径）。"""
        if raw_url.startswith("file:///"):
            path = raw_url[8:]
        elif raw_url.startswith("file://"):
            path = raw_url[7:]
        else:
            path = raw_url
        path = urllib.parse.unquote(path)
        return os.path.abspath(path)

    def dragEnterEvent(self, event: QDragEnterEvent):
        mime_data = event.mimeData()
        if mime_data.hasUrls():
            event.acceptProposedAction()
            self._hint_label.setText("✨ 松开放置文件")
            self._set_drag_active(True)
            logger.debug("拖拽进入放置区")
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent):
        self._hint_label.setText("将文件或文件夹拖拽到此处，或点击下方按钮直接选择")
        self._set_drag_active(False)

    def dropEvent(self, event: QDropEvent):
        mime_data = event.mimeData()
        self._set_drag_active(False)
        self._hint_label.setText("将文件或文件夹拖拽到此处，或点击下方按钮直接选择")
        if not mime_data.hasUrls():
            return
        urls = mime_data.urls()
        if not urls:
            return

        event.acceptProposedAction()
        clean_paths = [
            (u.toLocalFile() if hasattr(u, "isLocalFile") and u.isLocalFile() else self.clean_path_from_url(u.toString()))
            for u in urls
        ]
        logger.info(f"DropZone 接收到 {len(clean_paths)} 个路径: {clean_paths}")

        if self._on_paths_ready:
            self._on_paths_ready(clean_paths)
        elif self._on_path_ready:
            for p in clean_paths:
                self._on_path_ready(p)

        if len(clean_paths) == 1:
            self._hint_label.setText(f"已接收: {clean_paths[0]}")
        else:
            self._hint_label.setText(f"已接收 {len(clean_paths)} 个文件/文件夹")


class MainWindow(QMainWindow):
    """懒人解压😋 主窗口。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("懒人解压😋")
        self.resize(840, 600)
        self.setAcceptDrops(True)  # 支持全局窗口拖拽

        # ---- 核心层 ----
        self._config = ConfigManager()
        self._task_queue = TaskQueue()
        self._sandbox_mgr = SandboxManager()

        # ---- 信号绑定 ----
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
        self.drop_zone.set_paths_callback(self._on_drop_paths_ready)
        self.drop_zone.btn_select_files.clicked.connect(self._on_select_files_clicked)
        self.drop_zone.btn_select_dir.clicked.connect(self._on_select_dir_clicked)
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

        # 右键快捷菜单 & 双击行直接定位
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_table_context_menu)
        self._table.cellDoubleClicked.connect(self._on_cell_double_clicked)

        task_layout.addWidget(self._table)
        root.addWidget(task_group, stretch=1)

        # ---- 底部状态统计栏 (Statistics Bar) ----
        stat_panel = QWidget()
        stat_panel.setObjectName("StatBar")
        stat_layout = QHBoxLayout(stat_panel)
        stat_layout.setContentsMargins(10, 6, 10, 6)

        self._lbl_stats = QLabel("📊 总任务: 0 | 成功: 0 | 失败: 0 | 处理中: 0")
        self._lbl_stats.setObjectName("lblStats")

        self._cb_delete_source = QCheckBox("🗑️ 解压后移至回收站")
        self._cb_delete_source.setObjectName("cbDeleteSource")
        self._cb_delete_source.setToolTip("解压成功后自动将源压缩包（及关联同族分卷）安全移入 Windows 回收站")
        self._cb_delete_source.setChecked(bool(self._config.get("delete_source_after_extract", False)))
        self._cb_delete_source.toggled.connect(self._on_delete_source_toggled)

        self.btn_clean_finished = QPushButton("🧹 清空已完成")
        self.btn_clean_finished.setObjectName("btnCleanFinished")
        self.btn_clean_finished.setToolTip("从看板中移除所有成功或已跳过的任务")
        self.btn_clean_finished.clicked.connect(self._on_clean_finished_clicked)

        stat_layout.addWidget(self._lbl_stats)
        stat_layout.addStretch(1)
        stat_layout.addWidget(self._cb_delete_source)
        stat_layout.addWidget(self.btn_clean_finished)
        root.addWidget(stat_panel)

        # ---- 底部操作按钮区 ----
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

        # 映射与元数据记录
        # 行索引映射：{task_id: row_index}
        self._task_rows: dict[str, int] = {}
        # 任务详细数据：{task_id: {"src_path": ..., "filename": ..., "status": ..., "engine": ..., "dest_path": ..., "remark": ...}}
        self._task_data: dict[str, dict] = {}

        # 启动调度器
        self._task_queue.start()
        self._update_statistics()

    def closeEvent(self, event):
        self._task_queue.shutdown(wait=False)
        super().closeEvent(event)

    # ---------- 全局拖拽支持 ----------

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.drop_zone._set_drag_active(True)
            self.drop_zone._hint_label.setText("✨ 松开放置文件")
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent):
        self.drop_zone._set_drag_active(False)
        self.drop_zone._hint_label.setText("将文件或文件夹拖拽到此处，或点击下方按钮直接选择")
        event.accept()

    def dropEvent(self, event: QDropEvent):
        self.drop_zone._set_drag_active(False)
        self.drop_zone._hint_label.setText("将文件或文件夹拖拽到此处，或点击下方按钮直接选择")
        mime_data = event.mimeData()
        if not mime_data.hasUrls():
            return
        event.acceptProposedAction()
        clean_paths = [
            (u.toLocalFile() if hasattr(u, "isLocalFile") and u.isLocalFile() else DropZone.clean_path_from_url(u.toString()))
            for u in urls
        ]
        logger.info(f"主窗口全局拖拽接收到 {len(clean_paths)} 个路径: {clean_paths}")
        self._on_drop_paths_ready(clean_paths)

    # ---------- 手动选择按钮事件 ----------

    def _on_select_files_clicked(self):
        """点击 [📂 选择文件] 按钮。"""
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择待解压文件",
            "",
            "压缩包与伪装媒体 (*.zip *.rar *.7z *.tar *.gz *.bz2 *.mp4 *.jpg *.png *.pdf);;所有文件 (*.*)",
        )
        if not files:
            return
        clean_paths = [os.path.abspath(urllib.parse.unquote(f)) for f in files]
        logger.info(f"文件对话框选中 {len(clean_paths)} 个文件")
        self._on_drop_paths_ready(clean_paths)

    def _on_select_dir_clicked(self):
        """点击 [📁 选择目录] 按钮。"""
        selected_dir = QFileDialog.getExistingDirectory(self, "选择待扫描文件夹")
        if not selected_dir:
            return
        clean_dir = os.path.abspath(urllib.parse.unquote(selected_dir))
        logger.info(f"文件夹对话框选中目录: {clean_dir}")
        self._handle_directory_drop(clean_dir)

    # ---------- 路径调度与安检 ----------

    def _on_drop_paths_ready(self, clean_paths: list[str]):
        """批量处理拖入或选择的路径。"""
        for path in clean_paths:
            self._on_drop_path_ready(path)

    def _on_drop_path_ready(self, clean_path: str):
        """拖拽入口安检：严格分流文件与文件夹（铁律一）。"""
        if os.path.isfile(clean_path):
            task_id = str(uuid.uuid4())[:8]
            filename = os.path.basename(clean_path)
            self._task_data[task_id] = {
                "src_path": clean_path,
                "filename": filename,
                "status": "排队中",
                "engine": "",
                "dest_path": "",
                "remark": "",
            }

            # 受保护终点程序检查 (.apk, .exe, .msi)
            ext = os.path.splitext(clean_path)[1].lower()
            if ext in PROTECTED_ENDPOINT_EXTS:
                self._add_task_row(task_id, filename)
                self._set_cell(self._task_rows[task_id], 1, "已跳过")
                self._set_cell(self._task_rows[task_id], 3, f"受保护终点程序 ({ext})")
                self._task_data[task_id]["status"] = "已跳过"
                self._task_data[task_id]["remark"] = f"受保护终点程序 ({ext})"
                self._update_statistics()
                logger.info(f"终点白名单保护，跳过: {filename}")
                return

            # 非首卷分卷检查 (.part02+ 或 .002+)
            if is_secondary_volume(filename):
                self._add_task_row(task_id, filename)
                self._set_cell(self._task_rows[task_id], 1, "已跳过")
                self._set_cell(self._task_rows[task_id], 3, "非首卷分卷（由首卷统一解压）")
                self._task_data[task_id]["status"] = "已跳过"
                self._task_data[task_id]["remark"] = "非首卷分卷（由首卷统一解压）"
                self._update_statistics()
                logger.info(f"非首卷分卷，跳过单独解压: {filename}")
                return

            # 大小预检查
            if self._task_queue._is_file_too_small(clean_path):
                self._add_task_row(task_id, filename)
                self._set_cell(self._task_rows[task_id], 1, "已跳过(文件过小)")
                self._set_cell(self._task_rows[task_id], 3, "文件过小跳过")
                self._task_data[task_id]["status"] = "已跳过(文件过小)"
                self._update_statistics()
                logger.info(f"文件过小，跳过: {filename} ({os.path.getsize(clean_path)} bytes)")
                return

            self._add_task_row(task_id, filename)
            self._set_cell(self._task_rows[task_id], 1, "排队中")
            self._task_queue.submit_existing(task_id, clean_path)
            self._update_statistics()
        elif os.path.isdir(clean_path):
            # 文件夹：转交递归扫描器，绝不允许直接传给 shutil.copy
            self._handle_directory_drop(clean_path)
        else:
            QMessageBox.warning(self, "路径错误", f"路径不存在:\n{clean_path}")

    def _handle_directory_drop(self, dir_path: str):
        """将文件夹转交递归扫描器，并在状态栏提示。"""
        logger.info(f"启动文件夹递归扫描: {dir_path}")
        self.drop_zone._hint_label.setText(f"🔍 正在扫描文件夹: {dir_path}")
        self._task_queue.scan_directory(dir_path)
        self.drop_zone._hint_label.setText("将文件或文件夹拖拽到此处，或点击下方按钮直接选择")
        self._update_statistics()

    # ---------- QTableWidget 任务看板核心逻辑 ----------

    def _add_task_row(self, task_id: str, filename: str):
        row = self._table.rowCount()
        self._table.insertRow(row)
        for col in range(4):
            item = QTableWidgetItem(filename if col == 0 else "")
            item.setData(Qt.ItemDataRole.UserRole, task_id)
            self._table.setItem(row, col, item)
        self._task_rows[task_id] = row
        self._update_statistics()

    def _on_task_status_changed(self, task_id: str, filename: str, status: str, engine: str, remark: str):
        """信号驱动：原地更新表格行，绝不允许插入重复行（修复'分身'Bug）。"""
        row = self._task_rows.get(task_id)
        if row is None:
            # 首次接收到该任务状态（包含扫描发现的排队任务与直接跳过的分卷/小文件）：插入新行
            self._add_task_row(task_id, filename)
            row = self._task_rows[task_id]
            if task_id not in self._task_data:
                self._task_data[task_id] = {
                    "src_path": "",
                    "filename": filename,
                    "status": status,
                    "engine": engine,
                    "dest_path": "",
                    "remark": remark,
                }

        # 更新元数据记录
        if task_id in self._task_data:
            self._task_data[task_id]["status"] = status
            if engine:
                self._task_data[task_id]["engine"] = engine
            if remark:
                self._task_data[task_id]["remark"] = remark
                if status == "成功":
                    self._task_data[task_id]["dest_path"] = remark

        if filename:
            item = self._table.item(row, 0)
            if item:
                item.setText(filename)
        self._set_cell(row, 1, status)
        self._set_cell(row, 2, engine)
        self._set_cell(row, 3, remark)
        self._update_statistics()
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

    def _sync_task_rows(self):
        """重新同步 self._task_rows 映射。"""
        self._task_rows.clear()
        for r in range(self._table.rowCount()):
            item = self._table.item(r, 0)
            if item:
                tid = item.data(Qt.ItemDataRole.UserRole)
                if tid:
                    self._task_rows[tid] = r

    def _get_cell_text(self, row: int, col: int) -> str:
        item = self._table.item(row, col)
        return item.text() if item else ""

    # ---------- 双击行定位与右键菜单 ----------

    def _on_cell_double_clicked(self, row: int, column: int):
        """双击某个任务行：若该任务已完成（状态为“成功”），在资源管理器中定位打开。"""
        self._locate_task_target(row)

    def _locate_task_target(self, row: int):
        if row < 0 or row >= self._table.rowCount():
            return
        item_0 = self._table.item(row, 0)
        if not item_0:
            return
        task_id = item_0.data(Qt.ItemDataRole.UserRole)
        info = self._task_data.get(task_id, {})
        status = self._get_cell_text(row, 1)
        remark = self._get_cell_text(row, 3)

        if not status.startswith("成功"):
            return

        target_path = info.get("dest_path") or remark
        if not target_path:
            return

        target_path = os.path.normpath(target_path)
        if not os.path.exists(target_path):
            QMessageBox.information(self, "提示", f"目标路径不存在或已被移动:\n{target_path}")
            return

        try:
            if os.path.isdir(target_path):
                os.startfile(target_path)
            else:
                subprocess.Popen(["explorer", f"/select,{target_path}"])
            logger.info(f"已在资源管理器中定位打开: {target_path}")
        except Exception as e:
            logger.error(f"无法在资源管理器中定位路径 ({target_path}): {e}")
            QMessageBox.warning(self, "打开失败", f"无法打开路径:\n{target_path}\n错误: {e}")

    def _on_table_context_menu(self, pos: QPoint):
        """表格右键快捷菜单。"""
        row = self._table.rowAt(pos.y())
        if row < 0 or row >= self._table.rowCount():
            return

        item_0 = self._table.item(row, 0)
        if not item_0:
            return
        task_id = item_0.data(Qt.ItemDataRole.UserRole)
        info = self._task_data.get(task_id, {})
        status = self._get_cell_text(row, 1)
        remark = self._get_cell_text(row, 3)
        target_path = info.get("dest_path") or (remark if status.startswith("成功") else "")
        src_path = info.get("src_path", "")

        menu = QMenu(self)

        # 1. 📂 打开所在目录（调用 Explorer 打开该任务目标路径或所在目录）
        act_open = menu.addAction("📂 打开所在目录")
        has_valid_target = bool(target_path and os.path.exists(os.path.normpath(target_path)))
        has_valid_src = bool(src_path and os.path.exists(os.path.normpath(src_path)))
        act_open.setEnabled(has_valid_target or has_valid_src)

        # 2. 📋 复制输出路径（将路径复制到剪贴板）
        act_copy = menu.addAction("📋 复制输出路径")
        copy_candidate = target_path or (remark if status.startswith("成功") else "") or src_path
        act_copy.setEnabled(bool(copy_candidate))

        menu.addSeparator()

        # 3. 🔄 重新解压（若任务失败或跳过，支持重新入队）
        act_retry = menu.addAction("🔄 重新解压")
        can_retry = (status.startswith("失败") or "跳过" in status) and bool(src_path) and os.path.exists(src_path)
        act_retry.setEnabled(can_retry)

        # 4. 🗑️ 从列表中移除（将该行从表格中移除）
        act_remove = menu.addAction("🗑️ 从列表中移除")

        selected_action = menu.exec(self._table.viewport().mapToGlobal(pos))
        if not selected_action:
            return

        if selected_action == act_open:
            if has_valid_target:
                p = os.path.normpath(target_path)
                if os.path.isdir(p):
                    os.startfile(p)
                else:
                    subprocess.Popen(["explorer", f"/select,{p}"])
            elif has_valid_src:
                p = os.path.normpath(src_path)
                subprocess.Popen(["explorer", f"/select,{p}"])

        elif selected_action == act_copy:
            QGuiApplication.clipboard().setText(copy_candidate)
            logger.info(f"已复制路径到剪贴板: {copy_candidate}")

        elif selected_action == act_retry:
            self._retry_task(row, task_id)

        elif selected_action == act_remove:
            self._remove_task_row(row, task_id)

    def _retry_task(self, row: int, task_id: str):
        """重新解压任务。"""
        info = self._task_data.get(task_id, {})
        src_path = info.get("src_path", "")
        if not src_path or not os.path.exists(src_path):
            QMessageBox.warning(self, "错误", f"源文件不存在，无法重新解压:\n{src_path}")
            return

        filename = os.path.basename(src_path)
        # 大小预检查
        if self._task_queue._is_file_too_small(src_path):
            self._set_cell(row, 1, "已跳过(文件过小)")
            self._set_cell(row, 3, "文件过小跳过")
            self._task_data[task_id]["status"] = "已跳过(文件过小)"
            self._update_statistics()
            return

        new_task_id = str(uuid.uuid4())[:8]
        self._task_rows.pop(task_id, None)
        self._task_data.pop(task_id, None)

        self._task_rows[new_task_id] = row
        self._task_data[new_task_id] = {
            "src_path": src_path,
            "filename": filename,
            "status": "排队中",
            "engine": "",
            "dest_path": "",
            "remark": "准备重新解压...",
        }

        for col in range(4):
            item = self._table.item(row, col)
            if item:
                item.setData(Qt.ItemDataRole.UserRole, new_task_id)

        self._set_cell(row, 1, "排队中")
        self._set_cell(row, 2, "")
        self._set_cell(row, 3, "准备重新解压...")
        self._task_queue.submit_existing(new_task_id, src_path)
        self._update_statistics()
        logger.info(f"任务已重新提交入队 [old={task_id}, new={new_task_id}]: {src_path}")

    def _remove_task_row(self, row: int, task_id: str):
        """从表格中移除单行任务。"""
        self._task_rows.pop(task_id, None)
        self._task_data.pop(task_id, None)
        self._table.removeRow(row)
        self._sync_task_rows()
        self._update_statistics()
        logger.info(f"已从任务列表中移除: {task_id}")

    # ---------- 底部状态统计栏 ----------

    def _update_statistics(self):
        """刷新底部状态统计栏数据。"""
        total = self._table.rowCount()
        success = 0
        failed = 0
        in_progress = 0
        for r in range(total):
            item = self._table.item(r, 1)
            if not item:
                continue
            st = item.text()
            if st.startswith("成功"):
                success += 1
            elif st.startswith("失败"):
                failed += 1
            elif st in ("正在提取", "验证密码", "排队中") or "正在" in st:
                in_progress += 1

        self._lbl_stats.setText(
            f"📊 总任务: {total} | 成功: {success} | 失败: {failed} | 处理中: {in_progress}"
        )

    def _on_clean_finished_clicked(self):
        """点击 [🧹 清空已完成]：清理表格中所有“成功”或“已跳过”的行。"""
        cleaned_count = 0
        for r in range(self._table.rowCount() - 1, -1, -1):
            item_status = self._table.item(r, 1)
            if item_status:
                st = item_status.text()
                if st.startswith("成功") or "跳过" in st:
                    item_0 = self._table.item(r, 0)
                    if item_0:
                        tid = item_0.data(Qt.ItemDataRole.UserRole)
                        self._task_rows.pop(tid, None)
                        self._task_data.pop(tid, None)
                    self._table.removeRow(r)
                    cleaned_count += 1
        self._sync_task_rows()
        self._update_statistics()
        logger.info(f"清空已完成任务，共清理 {cleaned_count} 条记录")

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
        password, ok = QInputDialog.getText(
            self,
            "重新输入密码",
            "请再次输入密码：\n（取消则跳过此任务）",
        )
        if ok and password.strip():
            self._task_queue.supply_password(task_id, password.strip())
        else:
            self._task_queue.supply_password(task_id, None)

    # ---------- 底部操作按钮事件 ----------

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

    def _on_delete_source_toggled(self, checked: bool):
        self._config.set("delete_source_after_extract", checked)
        self._config.save()
        logger.info(f"源文件自动删除快捷开关变更为: {checked}")

    def _on_settings_clicked(self):
        logger.info("用户点击「全局设置」")
        from ui.settings_dialog import SettingsDialog
        dialog = SettingsDialog(self)
        if dialog.exec():
            # 用户点击了保存 → 重载调度器配置并同步快捷开关
            self._task_queue.reload_config()
            self._cb_delete_source.setChecked(
                bool(self._config.get("delete_source_after_extract", False))
            )
