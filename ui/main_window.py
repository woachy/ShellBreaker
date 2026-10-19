"""
便捷解压 — 主窗口（V2：40px 自绘标题栏 + 导航壳 + 任务看板）

表现层全部重写；业务逻辑（拖放分流、任务表同步、右键菜单、密码交互、
设置联动、统计）保持原有行为不变。
"""
from __future__ import annotations

import ctypes
import logging
import os
import subprocess
import sys
import urllib.parse
import uuid
from ctypes import wintypes

from PySide6.QtCore import QEvent, QPoint, Qt
from PySide6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QFont,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPaintEvent,
    QResizeEvent,
)
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.sandbox import PROTECTED_ENDPOINT_EXTS, SandboxManager, is_secondary_volume
from core.task_queue import TaskQueue
from data.config_manager import ConfigManager
from ui.pages.passwords_page import PasswordsPage
from ui.pages.settings_page import SettingsPage
from ui.pages.tasks_page import TasksPage
from ui.theme.icons import (
    ICON_CLEAN,
    ICON_FILE,
    ICON_FOLDER,
    ICON_REFRESH,
    app_icon,
    glyph_icon,
)
from ui.theme.theme_manager import ensure_theme, get_current_theme
from ui.theme.tokens import TITLE_BAR_HEIGHT
from ui.widgets.content_dialog import PasswordInputDialog
from ui.widgets.drop_card import DropCard
from ui.widgets.navigation import NavigationView
from ui.widgets.title_bar import TitleBar

logger = logging.getLogger("ShellBreaker.MainWindow")

# 任务状态 → 展示颜色（语义色由主题令牌提供，这里保留常量以便外部引用）
STATUS_COLORS = {
    "成功": "#0F7B0F",
    "失败": "#C42B1C",
    "正在提取": "#0E7490",
    "验证密码": "#9D5D00",
    "排队中": "#5D5D5D",
    "已跳过": "#9D5D00",
}
_DEFAULT_STATUS_COLOR = "#16212B"

# 无边框窗口的命中测试（Windows WM_NCHITTEST）
_WM_NCHITTEST = 0x0084
_HTCLIENT = 1
_RESIZE_BORDER = 6

# DWM：窗口圆角偏好 / 边框颜色
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_BORDER_COLOR = 34
_DWMWA_COLOR_NONE = 0xFFFFFFFE
_DWMWCP_ROUND = 2

# 无边框窗口补回原生风格 + 取消非客户区
_WM_NCCALCSIZE = 0x0083
_GWL_STYLE = -16
_WS_CAPTION = 0x00C00000
_WS_SYSMENU = 0x00080000
_WS_THICKFRAME = 0x00040000
_WS_MINIMIZEBOX = 0x00020000
_WS_MAXIMIZEBOX = 0x00010000
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020


class _MARGINS(ctypes.Structure):
    _fields_ = [
        ("cxLeftWidth", ctypes.c_int),
        ("cxRightWidth", ctypes.c_int),
        ("cyTopHeight", ctypes.c_int),
        ("cyBottomHeight", ctypes.c_int),
    ]


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


class DropZone(DropCard):
    """向后兼容的 DropZone 包装类。"""

    clean_path_from_url = staticmethod(clean_path_from_url)


class Shell(QFrame):
    """窗口外壳：绘制 V2 三段纵向渐变。"""

    def paintEvent(self, event: QPaintEvent):
        theme = get_current_theme()
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        grad = QLinearGradient(0, 0, 0, self.height())
        grad.setColorAt(0.0, QColor(theme.window_top))
        grad.setColorAt(0.55, QColor(theme.window))
        grad.setColorAt(1.0, QColor(theme.window_bottom))
        p.fillRect(self.rect(), grad)
        p.end()


class MainWindow(QMainWindow):
    """便捷解压 主窗口。"""

    def __init__(self):
        super().__init__()
        ensure_theme(self)

        self.setWindowTitle("便捷解压")
        self.setWindowIcon(app_icon())
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.resize(1160, 760)
        self.setMinimumSize(900, 600)
        self.setAcceptDrops(True)  # 全局窗口拖拽入口（唯一负责接收 dropEvent，防重复提交）

        # ---- 核心层 ----
        self._config = ConfigManager()
        self._task_queue = TaskQueue()
        self._sandbox_mgr = SandboxManager()

        # ---- 信号绑定 ----
        self._task_queue.task_status_changed.connect(self._on_task_status_changed)
        self._task_queue.task_progress.connect(self._on_task_progress)
        self._task_queue.password_needed.connect(self._on_password_needed)
        self._task_queue.password_wrong.connect(self._on_password_wrong)

        # ---- 外壳布局： [导航 | (标题栏 + 页面)] ----
        central = QWidget(self)
        central.setObjectName("Root")
        self.setCentralWidget(central)
        root_lay = QVBoxLayout(central)
        root_lay.setContentsMargins(0, 0, 0, 0)
        root_lay.setSpacing(0)

        self.shell = Shell(central)
        self.shell.setObjectName("Shell")
        root_lay.addWidget(self.shell)

        shell_lay = QHBoxLayout(self.shell)
        shell_lay.setContentsMargins(0, 0, 0, 0)
        shell_lay.setSpacing(0)

        self.nav = NavigationView(self.shell)
        self.nav.set_collapsed(self.width() < 1008)
        shell_lay.addWidget(self.nav)

        right = QWidget(self.shell)
        right_lay = QVBoxLayout(right)
        right_lay.setContentsMargins(0, 0, 0, 0)
        right_lay.setSpacing(0)

        self.title_bar = TitleBar(self, "", self.shell)
        self.title_bar.setFixedHeight(TITLE_BAR_HEIGHT)
        # 应用名由导航品牌行承载，标题栏只保留窗口按钮与拖动区
        self.title_bar.icon_label.hide()
        self.title_bar.title_label.hide()
        right_lay.addWidget(self.title_bar)

        self.stack = QStackedWidget(right)
        self.stack.setObjectName("Content")
        right_lay.addWidget(self.stack, 1)
        shell_lay.addWidget(right, 1)

        # ---- 页面（全部立即创建：控件构造后即可用，不做懒加载） ----
        self.tasks_page = TasksPage(self)
        self.stack.addWidget(self.tasks_page)

        self.passwords_page = PasswordsPage(self)
        self.stack.addWidget(self.passwords_page)

        self.settings_page = SettingsPage(self)
        self.settings_page.settingsChanged.connect(self._on_settings_page_changed)
        self.stack.addWidget(self.settings_page)

        # 导航切换联动
        self.nav.page_changed.connect(self._on_nav_page_changed)

        # ---- 兼容层：测试与业务代码强依赖的属性 ----
        self._table = self.tasks_page.table
        self._cb_delete_source = self.tasks_page.cb_delete_source
        self._lbl_stats = self.tasks_page.lbl_stats
        self.drop_zone = self.tasks_page.drop_card
        self.btn_clean_finished = self.tasks_page.btn_clean_finished

        # 绑定任务页操作事件
        self.tasks_page.drop_card.btn_select_files.clicked.connect(self._on_select_files_clicked)
        self.tasks_page.drop_card.btn_select_dir.clicked.connect(self._on_select_dir_clicked)
        self.tasks_page.btn_clean_finished.clicked.connect(self._on_clean_finished_clicked)
        self._table.customContextMenuRequested.connect(self._on_table_context_menu)
        self._table.cellDoubleClicked.connect(self._on_cell_double_clicked)

        # 初始化源文件删除状态
        self._cb_delete_source.setChecked(bool(self._config.get("delete_source_after_extract", False)))
        self._cb_delete_source.toggled.connect(self._on_delete_source_toggled)

        # ---- 兼容按钮（不进入布局，仅保留旧属性） ----
        self.btn_clear_cache = QPushButton("清空缓存", self)
        self.btn_password_mgr = QPushButton("密码管理", self)
        self.btn_settings = QPushButton("全局设置", self)
        for btn in (self.btn_clear_cache, self.btn_password_mgr, self.btn_settings):
            btn.hide()
        self.btn_clear_cache.clicked.connect(self._on_clear_cache_clicked)
        self.btn_password_mgr.clicked.connect(self._on_password_mgr_clicked)
        self.btn_settings.clicked.connect(self._on_settings_clicked)

        # ---- 标题栏拖动（导航品牌行同样可拖） ----
        for widget in (
            self.nav.brand_row,
            self.nav.app_icon_lbl,
            self.nav.app_title_lbl,
        ):
            widget.installEventFilter(self)

        # 映射与元数据记录
        self._task_rows: dict[str, int] = {}
        self._task_data: dict[str, dict] = {}

        # 启动调度器
        self._task_queue.start()
        self._update_statistics()

    # ---------- 窗口绘制与自适应 ----------

    def resizeEvent(self, event: QResizeEvent):
        """响应式布局：宽 < 1008px 时折叠导航为 48px 图标栏。"""
        super().resizeEvent(event)
        if hasattr(self, "nav"):
            self.nav.set_collapsed(self.width() < 1008)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(self, "title_bar"):
            self.title_bar.sync_state()

    def closeEvent(self, event):
        self._task_queue.shutdown(wait=False)
        super().closeEvent(event)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        if not getattr(self, "_native_frame_ready", False):
            self._native_frame_ready = True
            self._apply_native_frame()

    def _apply_native_frame(self):
        """让 DWM 负责圆角与窗口投影。

        自绘 1px 直角边框在高分屏上会显得“硬切”。做法是给无边框窗口补回
        WS_THICKFRAME/WS_CAPTION 风格（DWM 才会画圆角与阴影），再用
        WM_NCCALCSIZE 把非客户区清零，保证客户端仍然铺满整个窗口。
        """
        if sys.platform != "win32":
            return
        try:
            hwnd = wintypes.HWND(int(self.winId()))
            user32 = ctypes.windll.user32
            get_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            get_long.restype = ctypes.c_ssize_t
            set_long.restype = ctypes.c_ssize_t

            style = get_long(hwnd, _GWL_STYLE)
            style |= _WS_THICKFRAME | _WS_CAPTION | _WS_SYSMENU | _WS_MAXIMIZEBOX | _WS_MINIMIZEBOX
            set_long(hwnd, _GWL_STYLE, style)
            user32.SetWindowPos(
                hwnd,
                0,
                0,
                0,
                0,
                0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOZORDER | _SWP_FRAMECHANGED | _SWP_NOACTIVATE,
            )

            dwmapi = ctypes.windll.dwmapi
            corner = ctypes.c_int(_DWMWCP_ROUND)
            dwmapi.DwmSetWindowAttribute(
                hwnd, _DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(corner), ctypes.sizeof(corner)
            )
            # 去掉 DWM 的 1px 硬描边，只保留圆角 + 投影，避免边缘“硬切”
            border = ctypes.c_uint(_DWMWA_COLOR_NONE)
            dwmapi.DwmSetWindowAttribute(
                hwnd, _DWMWA_BORDER_COLOR, ctypes.byref(border), ctypes.sizeof(border)
            )
        except Exception:  # pragma: no cover - 老系统或非 DWM 环境静默降级
            pass

    # ---------- 无边框窗口：原生缩放 ----------
    def nativeEvent(self, event_type, message):
        if sys.platform == "win32" and event_type == b"windows_generic_MSG":
            try:
                msg = wintypes.MSG.from_address(int(message))
                # 把非客户区清零：保留 DWM 圆角/阴影，但不留系统边框
                if msg.message == _WM_NCCALCSIZE and msg.wParam:
                    return True, 0
                if msg.message == _WM_NCHITTEST and not (
                    self.isMaximized() or self.isFullScreen()
                ):
                    code = self._hit_test_code(msg.lParam)
                    if code is not None:
                        return True, code
            except Exception:  # pragma: no cover - 非 Windows 或消息结构不符
                pass
        return super().nativeEvent(event_type, message)

    def _hit_test_code(self, lparam: int) -> int | None:
        raw_x = ctypes.c_short(lparam & 0xFFFF).value
        raw_y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        # WM_NCHITTEST 的坐标是“物理像素”，而 Qt 的 mapFromGlobal 用逻辑像素。
        # 高分屏下不换算会把窗口内部误判成边框，导致所有按钮点击变成缩放窗口。
        dpr = self.devicePixelRatioF() or 1.0
        pos = self.mapFromGlobal(QPoint(round(raw_x / dpr), round(raw_y / dpr)))
        w, h = self.width(), self.height()
        b = _RESIZE_BORDER
        left = pos.x() < b
        right = pos.x() >= w - b
        top = pos.y() < b
        bottom = pos.y() >= h - b
        # 标题栏右上角的三个系统按钮不能被顶边缩放带吃掉
        if top and self._over_caption_buttons(pos):
            top = False
        if top and left:
            return 13  # HTTOPLEFT
        if top and right:
            return 14  # HTTOPRIGHT
        if bottom and left:
            return 16  # HTBOTTOMLEFT
        if bottom and right:
            return 17  # HTBOTTOMRIGHT
        if left:
            return 10  # HTLEFT
        if right:
            return 11  # HTRIGHT
        if top:
            return 12  # HTTOP
        if bottom:
            return 15  # HTBOTTOM
        return None if pos.x() >= 0 and pos.y() >= 0 else _HTCLIENT

    def _over_caption_buttons(self, pos: QPoint) -> bool:
        bar = self.title_bar
        if bar is None:
            return False
        left = bar.btn_min.mapTo(self, QPoint(0, 0)).x()
        right = bar.btn_close.mapTo(self, QPoint(bar.btn_close.width(), 0)).x()
        return left <= pos.x() <= right

    def eventFilter(self, obj, event):  # noqa: N802
        """导航品牌行 = 标题栏拖动区。"""
        if obj in (self.nav.brand_row, self.nav.app_icon_lbl, self.nav.app_title_lbl):
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                handle = self.windowHandle()
                if handle is not None and handle.startSystemMove():
                    return True
            if event.type() == QEvent.Type.MouseButtonDblClick:
                self.title_bar.toggle_maximize()
                return True
        return super().eventFilter(obj, event)

    # ---------- 导航 ----------

    def _on_nav_page_changed(self, index: int):
        """响应左侧导航切换。"""
        if index == 1:
            self.passwords_page.refresh_table()
            self.stack.setCurrentIndex(1)
        elif index == 2:
            self.settings_page.reload_from_config()
            self.stack.setCurrentIndex(2)
        else:
            self.stack.setCurrentIndex(0)

    # ---------- 全局拖拽支持（唯一 drop target，绝不双重触发） ----------

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.tasks_page.drop_card.set_drag_active(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event: QDragLeaveEvent):
        self.tasks_page.drop_card.set_drag_active(False)
        event.accept()

    def dropEvent(self, event: QDropEvent):
        self.tasks_page.drop_card.set_drag_active(False)
        mime_data = event.mimeData()
        if not mime_data.hasUrls():
            return
        urls = mime_data.urls()
        if not urls:
            return
        event.acceptProposedAction()
        clean_paths = [
            (u.toLocalFile() if hasattr(u, "isLocalFile") and u.isLocalFile() else clean_path_from_url(u.toString()))
            for u in urls
        ]
        logger.info(f"主窗口全局拖拽接收到 {len(clean_paths)} 个路径: {clean_paths}")
        self._on_drop_paths_ready(clean_paths)
        if len(clean_paths) == 1:
            self.tasks_page.drop_card.set_received_hint(f"已接收: {clean_paths[0]}")
        else:
            self.tasks_page.drop_card.set_received_hint(f"已接收 {len(clean_paths)} 个文件/文件夹")

    # ---------- 手动选择按钮事件 ----------

    def _on_select_files_clicked(self):
        """点击 [选择文件] 按钮。"""
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
        """点击 [选择文件夹] 按钮。"""
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
            self._handle_directory_drop(clean_path)
        else:
            QMessageBox.warning(self, "路径错误", f"路径不存在:\n{clean_path}")

    def _handle_directory_drop(self, dir_path: str):
        """将文件夹转交递归扫描器。"""
        logger.info(f"启动文件夹递归扫描: {dir_path}")
        self.tasks_page.drop_card.set_received_hint(f"🔍 正在扫描文件夹: {dir_path}")
        self._task_queue.scan_directory(dir_path)
        self.tasks_page.drop_card.reset_hint()
        self._update_statistics()

    # ---------- 任务表核心逻辑 ----------

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
        """信号驱动：原地更新表格行，绝不插入重复行。状态文本原样保留不归一化。"""
        row = self._task_rows.get(task_id)
        if row is None:
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

        if col == 1:
            # 状态列由 StatusPillDelegate 负责着色；这里只保留加粗语义
            font = item.font()
            font.setBold(True)
            item.setFont(font)

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
        """双击已完成任务行：在资源管理器中定位打开。"""
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
        theme = get_current_theme()
        task_id = item_0.data(Qt.ItemDataRole.UserRole)
        info = self._task_data.get(task_id, {})
        status = self._get_cell_text(row, 1)
        remark = self._get_cell_text(row, 3)
        target_path = info.get("dest_path") or (remark if status.startswith("成功") else "")
        src_path = info.get("src_path", "")

        menu = QMenu(self)

        # 1. 打开所在目录
        act_open = menu.addAction(glyph_icon(ICON_FOLDER, theme.text2, 16), "打开所在目录")
        has_valid_target = bool(target_path and os.path.exists(os.path.normpath(target_path)))
        has_valid_src = bool(src_path and os.path.exists(os.path.normpath(src_path)))
        act_open.setEnabled(has_valid_target or has_valid_src)

        # 2. 复制输出路径
        act_copy = menu.addAction(glyph_icon(ICON_FILE, theme.text2, 16), "复制输出路径")
        copy_candidate = target_path or (remark if status.startswith("成功") else "") or src_path
        act_copy.setEnabled(bool(copy_candidate))

        menu.addSeparator()

        # 3. 重新解压
        act_retry = menu.addAction(glyph_icon(ICON_REFRESH, theme.text2, 16), "重新解压")
        can_retry = (status.startswith("失败") or "跳过" in status) and bool(src_path) and os.path.exists(src_path)
        act_retry.setEnabled(can_retry)

        # 4. 从列表中移除
        act_remove = menu.addAction(glyph_icon(ICON_CLEAN, theme.danger, 16), "从列表中移除")

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
        """刷新底部状态统计栏数据与任务空态。"""
        total = self._table.rowCount()
        success = 0
        failed = 0
        in_progress = 0
        skipped = 0
        for r in range(total):
            st = self._get_cell_text(r, 1)
            if not st:
                continue
            if st.startswith("成功"):
                success += 1
            elif st.startswith("失败"):
                failed += 1
            elif "跳过" in st:
                skipped += 1
            elif st in ("正在提取", "验证密码", "排队中") or "正在" in st:
                in_progress += 1

        self._lbl_stats.setText(f"共 {total} 个任务")
        self.tasks_page.status_bar.set_counts(total, success, failed, in_progress, skipped)
        self.tasks_page.update_empty_state()

    def _on_clean_finished_clicked(self):
        """点击 [清空已完成]：清理表格中所有“成功”或“已跳过”的行。"""
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

    # ---------- 密码自学习与对话框 ----------

    def _on_password_needed(self, task_id: str, archive_name: str):
        logger.info(f"[{task_id}] 弹出密码输入框: {archive_name}")
        dlg = PasswordInputDialog(archive_name, is_retry=False, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            pwd = dlg.get_password()
            if pwd:
                self._task_queue.supply_password(task_id, pwd)
                return
        self._task_queue.supply_password(task_id, None)

    def _on_password_wrong(self, task_id: str):
        """密码错误 → 弹出错误提示，再次请求输入。"""
        logger.info(f"[{task_id}] 密码错误，弹出重试密码框")
        task_info = self._task_data.get(task_id, {})
        archive_name = task_info.get("filename", "未知文件")
        dlg = PasswordInputDialog(archive_name, is_retry=True, parent=self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            pwd = dlg.get_password()
            if pwd:
                self._task_queue.supply_password(task_id, pwd)
                return
        self._task_queue.supply_password(task_id, None)

    # ---------- 菜单与动作事件 ----------

    def _on_clear_cache_clicked(self):
        logger.info("用户点击「清空缓存」")
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
        logger.info("切换到「密码管理」页面")
        self.nav.set_current_index(1)
        self.passwords_page.refresh_table()
        self.stack.setCurrentIndex(1)

    def _on_delete_source_toggled(self, checked: bool):
        self._config.set("delete_source_after_extract", checked)
        self._config.save()
        logger.info(f"源文件自动删除快捷开关变更为: {checked}")
        if hasattr(self, "settings_page"):
            cb = self.settings_page.cb_delete_source
            cb.blockSignals(True)
            cb.setChecked(checked)
            cb.blockSignals(False)

    def _on_settings_clicked(self):
        logger.info("切换到「全局设置」页面")
        self.nav.set_current_index(2)
        self.settings_page.reload_from_config()
        self.stack.setCurrentIndex(2)

    def _on_settings_page_changed(self, key: str):
        """响应设置页的数据修改。"""
        if key in ("default_engine", "max_workers", "bandizip_path", "winrar_path", "reset_defaults", "flush"):
            self._task_queue.reload_config()
        if key in ("delete_source_after_extract", "reset_defaults", "flush"):
            is_del = bool(self._config.get("delete_source_after_extract", False))
            self._cb_delete_source.blockSignals(True)
            self._cb_delete_source.setChecked(is_del)
            self._cb_delete_source.blockSignals(False)
