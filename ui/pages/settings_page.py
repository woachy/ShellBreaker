"""设置页（V2）：五个分组 + 每个控件即时自动保存。"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.sandbox import SandboxManager
from data.config_manager import ConfigManager
from ui.theme.fonts import display_font
from ui.theme.icons import ICON_DELETE, ICON_REFRESH, glyph_icon
from ui.theme.theme_manager import ensure_theme, get_current_theme, repolish
from ui.widgets.centered_column import CenteredColumn
from ui.widgets.combo_box import combo_with_items
from ui.widgets.number_box import NumberBox
from ui.widgets.path_picker import PathPicker
from ui.widgets.setting_row import SettingRow
from ui.widgets.settings_group import SettingsGroup
from ui.widgets.smooth_label import SmoothLabel
from ui.widgets.toggle_switch import ToggleSwitch

SAVED_TEXT = "✓ 所有更改已保存"
SAVING_TEXT = "正在保存…"
INVALID_TEXT = "路径无效，尚未保存"
RESET_TEXT = "已恢复默认设置"

ENGINE_ITEMS = [("Bandizip (bz.exe)", "bandizip"), ("WinRAR (WinRAR.exe)", "winrar")]
OUTPUT_ITEMS = [("源文件同级目录", "source_directory"), ("自定义目录", "custom")]


class SettingsPage(QWidget):
    """设置页。任何控件变更立即写入单个配置键并保存。"""

    settingsChanged = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        ensure_theme(self)
        self.setObjectName("SettingsPage")

        self._config = ConfigManager()
        self._sandbox_mgr = SandboxManager()
        self._is_loading = True

        # 并发数防抖：写盘立即，reload_config 延迟 400ms 只发一次
        self._reload_timer = QTimer(self)
        self._reload_timer.setSingleShot(True)
        self._reload_timer.setInterval(400)
        self._reload_timer.timeout.connect(lambda: self.settingsChanged.emit("max_workers"))

        self._reset_status_timer = QTimer(self)
        self._reset_status_timer.setSingleShot(True)
        self._reset_status_timer.setInterval(2000)
        self._reset_status_timer.timeout.connect(self._set_status_saved)

        self._build_ui()
        self.reload_from_config()
        self._is_loading = False

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("ScrollArea")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self.scroll)

        holder = CenteredColumn(parent=self.scroll)
        self.column = holder.body
        self.column.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)
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
        self.title_label = SmoothLabel("设置", self.column, get_current_theme().text)
        self.title_label.setObjectName("PageTitle")
        self.title_label.setFont(display_font(28))
        title_col.addWidget(self.title_label)
        self.subtitle_label = QLabel("更改会立即保存；路径无效时不会写入配置。", self.column)
        self.subtitle_label.setObjectName("PageSubtitle")
        title_col.addWidget(self.subtitle_label)
        header.addLayout(title_col, 1)

        self.status_label = QLabel(SAVED_TEXT, self.column)
        self.status_label.setObjectName("SavedStatus")
        self.status_label.setProperty("state", "saved")
        header.addWidget(self.status_label, 0, Qt.AlignmentFlag.AlignTop)
        col.addLayout(header)

        # ---- 分组：引擎 ----
        self.grp_engine = SettingsGroup("引擎", "选择解压内核，并手动指定可执行文件位置。", self.column)
        self.combo_engine = combo_with_items([], self)
        for text, data in ENGINE_ITEMS:
            self.combo_engine.addItem(text, data)
        self.row_engine = SettingRow("默认执行引擎", "自动解压时优先调用的程序", self.combo_engine)
        self.grp_engine.add_row(self.row_engine)

        self.picker_bandizip = PathPicker("选择 Bandizip 可执行文件", PathPicker.FILE, "留空则自动检测")
        self.row_bandizip = SettingRow("Bandizip 路径", "bz.exe 完整路径，留空自动检测", self.picker_bandizip)
        self.grp_engine.add_row(self.row_bandizip)

        self.picker_winrar = PathPicker("选择 WinRAR 可执行文件", PathPicker.FILE, "留空则自动检测")
        self.row_winrar = SettingRow("WinRAR 路径", "WinRAR.exe 完整路径，留空自动检测", self.picker_winrar)
        self.grp_engine.add_row(self.row_winrar)
        col.addWidget(self.grp_engine)

        # ---- 分组：输出 ----
        self.grp_output = SettingsGroup("输出", "设置解压产物的落盘位置。", self.column)
        self.combo_output = combo_with_items([], self)
        for text, data in OUTPUT_ITEMS:
            self.combo_output.addItem(text, data)
        self.row_output = SettingRow("输出位置", "解压结果默认保存到哪里", self.combo_output)
        self.grp_output.add_row(self.row_output)

        self.picker_custom = PathPicker("选择自定义输出目录", PathPicker.DIR, "源目录模式下无需填写")
        self.row_custom = SettingRow("自定义输出目录", "仅在输出位置选择“自定义目录”时生效", self.picker_custom)
        self.grp_output.add_row(self.row_custom)
        col.addWidget(self.grp_output)

        # ---- 分组：性能与过滤 ----
        self.grp_perf = SettingsGroup("性能与过滤", "控制并发与扫描深度，避免一次性压垮磁盘。", self.column)
        self.spin_workers = NumberBox(1, 16, 2, " 个", self)
        self.row_workers = SettingRow("并发任务数", "同时进行的解压任务数量（1–16）", self.spin_workers)
        self.grp_perf.add_row(self.row_workers)

        self.spin_scan_depth = NumberBox(1, 50, 4, " 层", self)
        self.row_scan_depth = SettingRow("最大扫描层数", "伪装文件递归探测的最大层数（1–50）", self.spin_scan_depth)
        self.grp_perf.add_row(self.row_scan_depth)

        self.spin_min_size = NumberBox(1, 10000, 50, " MB", self)
        self.row_min_size = SettingRow("最小解压文件大小", "小于该值的文件会被跳过，不参与解压", self.spin_min_size)
        self.grp_perf.add_row(self.row_min_size)
        col.addWidget(self.grp_perf)

        # ---- 分组：行为 ----
        self.grp_behavior = SettingsGroup("行为", "解压完成后的收尾动作。", self.column)
        self.cb_delete_source = ToggleSwitch("", self)
        self.row_delete_source = SettingRow(
            "解压成功后移至回收站",
            "把源压缩包放入回收站，可随时还原",
            self.cb_delete_source,
        )
        self.grp_behavior.add_row(self.row_delete_source)
        col.addWidget(self.grp_behavior)

        # ---- 分组：高级 ----
        self.grp_advanced = SettingsGroup("高级", "维护类操作，执行前会再次确认。", self.column)

        self.btn_clean_cache = QPushButton("清空缓存…", self)
        self.btn_clean_cache.setObjectName("DangerSubtle")
        self.btn_clean_cache.setCursor(Qt.CursorShape.PointingHandCursor)
        self.row_clean_cache = SettingRow(
            "清理临时文件",
            "删除所有沙箱临时目录，释放磁盘空间",
            self.btn_clean_cache,
        )
        self.grp_advanced.add_row(self.row_clean_cache)

        self.btn_reset_defaults = QPushButton("恢复默认", self)
        self.btn_reset_defaults.setObjectName("Subtle")
        self.btn_reset_defaults.setCursor(Qt.CursorShape.PointingHandCursor)
        self.row_reset = SettingRow(
            "恢复默认设置",
            "所有选项回到初始值，此操作不可撤销",
            self.btn_reset_defaults,
        )
        self.grp_advanced.add_row(self.row_reset)
        col.addWidget(self.grp_advanced)
        col.addStretch(1)

        h_lay = None  # 兼容：列容器由 CenteredColumn 负责
        self.scroll.setWidget(holder)

        # ---- 信号 ----
        self.combo_engine.currentIndexChanged.connect(self._on_engine_changed)
        self.combo_output.currentIndexChanged.connect(self._on_output_mode_changed)
        self.picker_bandizip.path_changed.connect(self._on_bandizip_path_changed)
        self.picker_winrar.path_changed.connect(self._on_winrar_path_changed)
        self.picker_custom.path_changed.connect(self._on_custom_path_changed)
        self.spin_workers.valueChanged.connect(self._on_workers_changed)
        self.spin_scan_depth.valueChanged.connect(self._on_scan_depth_changed)
        self.spin_min_size.valueChanged.connect(self._on_min_size_changed)
        self.cb_delete_source.toggled.connect(self._on_delete_source_toggled)
        self.btn_clean_cache.clicked.connect(self._on_clean_cache_clicked)
        self.btn_reset_defaults.clicked.connect(self._on_reset_defaults_clicked)

        self._refresh_icons()

    def _refresh_icons(self):
        theme = get_current_theme()
        self.btn_clean_cache.setIcon(glyph_icon(ICON_DELETE, theme.danger, 16))
        self.btn_reset_defaults.setIcon(glyph_icon(ICON_REFRESH, theme.text2, 16))

    # 兼容别名（旧代码/测试使用 edit_* 名称）
    @property
    def edit_bandizip_path(self):
        return self.picker_bandizip.line_edit

    @property
    def edit_winrar_path(self):
        return self.picker_winrar.line_edit

    @property
    def edit_custom_path(self):
        return self.picker_custom.line_edit

    # ------------------------------------------------------------- 状态机
    def _set_status(self, text: str, state: str):
        self.status_label.setText(text)
        self.status_label.setProperty("state", state)
        repolish(self.status_label)

    def _set_status_saved(self):
        self._set_status(SAVED_TEXT, "saved")

    def _set_status_saving(self):
        self._set_status(SAVING_TEXT, "saving")

    def _set_status_invalid(self):
        self._set_status(INVALID_TEXT, "invalid")

    def _set_status_reset(self):
        self._set_status(RESET_TEXT, "saving")
        self._reset_status_timer.start()

    # ------------------------------------------------------------- 读写
    def _commit(self, key: str, value):
        """只写一个键并立即落盘，然后广播变更。"""
        self._set_status_saving()
        self._config.set(key, value)
        self._config.save()
        self._set_status_saved()
        self.settingsChanged.emit(key)

    def reload_from_config(self):
        """从配置回填所有控件（不触发写盘）。"""
        self._is_loading = True
        try:
            cfg = self._config.get_all()

            idx = self.combo_engine.findData(cfg.get("default_engine", "bandizip"))
            self.combo_engine.setCurrentIndex(idx if idx >= 0 else 0)

            mode = cfg.get("output_mode", "source_directory")
            out_idx = self.combo_output.findData(mode)
            self.combo_output.setCurrentIndex(out_idx if out_idx >= 0 else 0)

            self.picker_bandizip.set_path(cfg.get("bandizip_path", ""))
            self.picker_winrar.set_path(cfg.get("winrar_path", ""))
            self.picker_custom.set_path(cfg.get("custom_output_path", ""))
            self.row_bandizip.set_error("")
            self.row_winrar.set_error("")
            self.row_custom.set_error("")

            self.spin_workers.setValue(int(cfg.get("max_workers", 2) or 2))
            self.spin_scan_depth.setValue(int(cfg.get("max_scan_depth", 4) or 4))
            self.spin_min_size.setValue(int(cfg.get("min_file_size_mb", 50) or 50))

            self.cb_delete_source.setChecked(bool(cfg.get("delete_source_after_extract", False)))
            self._sync_custom_state()
            self._set_status_saved()
        finally:
            self._is_loading = False

    def _sync_custom_state(self):
        is_custom = self.combo_output.currentData() == "custom"
        self.row_custom.set_muted(not is_custom)
        if is_custom:
            self.row_custom.desc_label.setText("解压产物将保存到该目录")
        else:
            self.row_custom.desc_label.setText("仅在输出位置选择“自定义目录”时生效")

    # ------------------------------------------------------------- 控件事件
    def _on_engine_changed(self, _index: int):
        if self._is_loading:
            return
        value = self.combo_engine.currentData()
        if not value:
            return
        self._commit("default_engine", value)

    def _on_output_mode_changed(self, _index: int):
        if self._is_loading:
            return
        mode = self.combo_output.currentData() or "source_directory"
        self._sync_custom_state()
        if mode == "custom":
            path = self.picker_custom.path()
            if not path or not os.path.isdir(path):
                # 切到自定义但目录尚不可用：只切换模式，不写坏路径
                self._commit("output_mode", mode)
                if self.row_custom.control is not None:
                    self.row_custom.control.setEnabled(True)
                self.picker_custom.btn_browse.setFocus()
                return
        self._commit("output_mode", mode)

    def _on_bandizip_path_changed(self, path: str):
        if self._is_loading:
            return
        if path and not os.path.isfile(path):
            self.row_bandizip.set_error("路径无效：找不到该文件")
            self._set_status_invalid()
            return
        self.row_bandizip.set_error("")
        self._commit("bandizip_path", path)

    def _on_winrar_path_changed(self, path: str):
        if self._is_loading:
            return
        if path and not os.path.isfile(path):
            self.row_winrar.set_error("路径无效：找不到该文件")
            self._set_status_invalid()
            return
        self.row_winrar.set_error("")
        self._commit("winrar_path", path)

    def _on_custom_path_changed(self, path: str):
        if self._is_loading:
            return
        if path and not os.path.isdir(path):
            self.row_custom.set_error("目录不存在")
            self._set_status_invalid()
            return
        self.row_custom.set_error("")
        self._commit("custom_output_path", path)

    def _on_workers_changed(self, value: int):
        if self._is_loading:
            return
        self._config.set("max_workers", int(value))
        self._config.save()
        self._set_status_saved()
        self._reload_timer.start()

    def _on_scan_depth_changed(self, value: int):
        if self._is_loading:
            return
        self._commit("max_scan_depth", int(value))

    def _on_min_size_changed(self, value: int):
        if self._is_loading:
            return
        self._commit("min_file_size_mb", int(value))

    def _on_delete_source_toggled(self, checked: bool):
        if self._is_loading:
            return
        self._commit("delete_source_after_extract", bool(checked))

    def _on_clean_cache_clicked(self):
        reply = QMessageBox.question(
            self,
            "确认清空",
            "确定要清除所有沙箱临时文件吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        count = self._sandbox_mgr.clean_all()
        QMessageBox.information(self, "完成", f"已清空 {count} 个临时沙箱目录")

    def _on_reset_defaults_clicked(self):
        reply = QMessageBox.question(
            self,
            "恢复默认设置？",
            "所有选项将恢复为初始值，此操作不可撤销。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._config.reset_defaults()
        self.reload_from_config()
        self._set_status_reset()
        self.settingsChanged.emit("reset_defaults")

    # ------------------------------------------------------------- 兼容层
    def flush(self) -> bool:
        """写回全部控件当前值（无论是否 dirty）。校验失败时不写盘。"""
        ok = True
        bz = self.picker_bandizip.path()
        if bz and not os.path.isfile(bz):
            self.row_bandizip.set_error("路径无效：找不到该文件")
            ok = False
        else:
            self.row_bandizip.set_error("")

        wr = self.picker_winrar.path()
        if wr and not os.path.isfile(wr):
            self.row_winrar.set_error("路径无效：找不到该文件")
            ok = False
        else:
            self.row_winrar.set_error("")

        mode = self.combo_output.currentData() or "source_directory"
        custom = self.picker_custom.path()
        if mode == "custom" and (not custom or not os.path.isdir(custom)):
            self.row_custom.set_error("目录不存在，无法保存")
            ok = False
        else:
            self.row_custom.set_error("")

        if not ok:
            self._set_status_invalid()
            return False

        self._set_status_saving()
        self._config.set("default_engine", self.combo_engine.currentData() or "bandizip")
        self._config.set("output_mode", mode)
        self._config.set("custom_output_path", custom)
        self._config.set("bandizip_path", bz)
        self._config.set("winrar_path", wr)
        self._config.set("max_workers", int(self.spin_workers.value()))
        self._config.set("max_scan_depth", int(self.spin_scan_depth.value()))
        self._config.set("min_file_size_mb", int(self.spin_min_size.value()))
        self._config.set("delete_source_after_extract", bool(self.cb_delete_source.isChecked()))
        self._config.save()
        self._set_status_saved()
        self.settingsChanged.emit("flush")
        return True
