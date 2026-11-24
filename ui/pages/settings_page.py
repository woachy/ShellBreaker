"""设置页（V2）：分组卡片 + 每个控件即时自动保存。"""
from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.engine import resolve_bandizip_cli
from core.sandbox import SandboxManager
from data.config_manager import ConfigManager
from ui.theme.fonts import display_font
from ui.theme.icons import ICON_DELETE, ICON_REFRESH, glyph_icon
from ui.theme.theme_manager import (
    ensure_theme,
    get_current_theme,
    get_theme_notifier,
    repolish,
    set_theme,
)
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

ENGINE_ITEMS = [
    ("Bandizip (bz.exe)", "bandizip"),
    ("WinRAR (WinRAR.exe)", "winrar"),
    ("7-Zip（内置 / 系统）", "7zip"),
]
OUTPUT_ITEMS = [("源文件同级目录", "source_directory"), ("自定义目录", "custom")]
DELETE_SOURCE_MODE_ITEMS = [
    ("不删除源文件", "keep"),
    ("移至 Windows 回收站（推荐）", "recycle_bin"),
    ("永久物理删除（不可恢复）", "permanent"),
]
THEME_ITEMS = [("浅色模式", "light"), ("深色模式", "dark")]


def parse_ad_filter_patterns(raw_text: str) -> list[str]:
    """将逗号分隔的推广文件过滤规则清洗为非空去重列表（保持原顺序）。"""
    normalized = str(raw_text or "").replace("，", ",").replace("\n", ",")
    seen: set[str] = set()
    result: list[str] = []
    for part in normalized.split(","):
        item = part.strip()
        if not item:
            continue
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


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

        get_theme_notifier().theme_changed.connect(self._on_theme_changed)

    def closeEvent(self, event):  # noqa: N802
        self._reload_timer.stop()
        self._reset_status_timer.stop()
        try:
            get_theme_notifier().theme_changed.disconnect(self._on_theme_changed)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(event)

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

        self.picker_sevenzip = PathPicker(
            "选择 7-Zip 可执行文件", PathPicker.FILE, "留空自动使用内置/系统 7-Zip"
        )
        self.row_sevenzip = SettingRow(
            "7-Zip (7z.exe / 7za.exe) 路径",
            "留空自动使用内置/系统 7-Zip",
            self.picker_sevenzip,
        )
        self.grp_engine.add_row(self.row_sevenzip)
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
        self.row_scan_depth = SettingRow("最大扫描层数", "拖入文件夹时递归扫描子目录的最大层数（1–50）", self.spin_scan_depth)
        self.grp_perf.add_row(self.row_scan_depth)

        self.spin_min_size = NumberBox(0, 10000, 50, " MB", self)
        self.row_min_size = SettingRow(
            "最小解压文件大小",
            "小于该体积的文件将被跳过（适用于拖入文件夹扫描与嵌套伪装探测，设为 0 表示不限制）",
            self.spin_min_size,
        )
        self.grp_perf.add_row(self.row_min_size)
        col.addWidget(self.grp_perf)

        # ---- 分组：清理与安全 ----
        self.grp_behavior = SettingsGroup("清理与安全", "解压完成后的源文件清理策略与推广垃圾文件过滤。", self.column)

        self.combo_delete_source_mode = combo_with_items(DELETE_SOURCE_MODE_ITEMS, self)
        self.combo_delete_source_mode.setMinimumWidth(220)
        self.row_delete_source = SettingRow(
            "解压成功后处理源文件",
            "控制解压成功后对源压缩包与分卷的清理方式",
            self.combo_delete_source_mode,
        )
        self.grp_behavior.add_row(self.row_delete_source)

        # 向后兼容隐藏开关：与 combo_delete_source_mode 双向同步，确保旧代码与单测无缝兼容
        self.cb_delete_source = ToggleSwitch("", self)
        self.cb_delete_source.hide()

        self.cb_ad_filter = ToggleSwitch("", self)
        self.row_ad_filter = SettingRow(
            "推广垃圾文件过滤",
            "交付前自动清理网址快捷方式、扫码关注等常见推广文件",
            self.cb_ad_filter,
        )
        self.grp_behavior.add_row(self.row_ad_filter)

        self.edit_ad_patterns = QLineEdit(self)
        self.edit_ad_patterns.setPlaceholderText("*.url, *扫码关注*, *资源发布页*")
        self.edit_ad_patterns.setMinimumWidth(240)
        self.edit_ad_patterns.setMaximumWidth(360)
        self.row_ad_patterns = SettingRow(
            "推广文件匹配规则",
            "逗号分隔通配符规则，回车或失去焦点自动清洗保存",
            self.edit_ad_patterns,
        )
        self.grp_behavior.add_row(self.row_ad_patterns)
        col.addWidget(self.grp_behavior)

        # ---- 分组：系统集成与自动化 ----
        self.grp_system = SettingsGroup(
            "系统集成与自动化",
            "配置监听目录自动解压，新增压缩包写入稳定后自动加入解压队列。",
            self.column,
        )

        self.cb_watch_folder = ToggleSwitch("", self)
        self.row_watch_folder = SettingRow(
            "监听文件夹自动解压",
            "自动监控指定目录新增压缩包，写入稳定后静默加入解压队列",
            self.cb_watch_folder,
        )
        self.grp_system.add_row(self.row_watch_folder)

        self.picker_watch_folder = PathPicker(
            "选择监听文件夹", PathPicker.DIR, "选择需要自动监听的目录"
        )
        self.row_watch_folder_path = SettingRow(
            "监听目录路径",
            "仅在开启“监听文件夹自动解压”后生效",
            self.picker_watch_folder,
        )
        self.grp_system.add_row(self.row_watch_folder_path)
        col.addWidget(self.grp_system)

        # ---- 分组：外观 ----
        self.grp_appearance = SettingsGroup("外观", "实时切换浅色或深色界面主题。", self.column)
        self.combo_theme = combo_with_items(THEME_ITEMS, self)
        self.row_theme = SettingRow("界面主题", "切换后立即生效并自动持久化保存", self.combo_theme)
        self.grp_appearance.add_row(self.row_theme)
        col.addWidget(self.grp_appearance)

        # ---- 分组：高级选项 ----
        self.grp_advanced = SettingsGroup("高级选项", "嵌套解压保护与维护类操作。", self.column)

        self.cb_nested_limit_confirm = ToggleSwitch("", self)
        self.row_nested_limit_confirm = SettingRow(
            "开启反复解压次数限制",
            "连续多层嵌套解压达到阈值时暂停并弹窗询问是否继续",
            self.cb_nested_limit_confirm,
        )
        self.grp_advanced.add_row(self.row_nested_limit_confirm)

        self.spin_nested_confirm_limit = NumberBox(3, 50, 10, " 次", self)
        self.row_nested_confirm_limit = SettingRow(
            "单轮最大连续解压次数",
            "达到该次数后触发确认弹窗，选择继续将重置本轮计数（3–50）",
            self.spin_nested_confirm_limit,
        )
        self.grp_advanced.add_row(self.row_nested_confirm_limit)

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

        self.scroll.setWidget(holder)

        # ---- 信号 ----
        self.combo_engine.currentIndexChanged.connect(self._on_engine_changed)
        self.combo_output.currentIndexChanged.connect(self._on_output_mode_changed)
        self.picker_bandizip.path_changed.connect(self._on_bandizip_path_changed)
        self.picker_winrar.path_changed.connect(self._on_winrar_path_changed)
        self.picker_sevenzip.path_changed.connect(self._on_sevenzip_path_changed)
        self.picker_custom.path_changed.connect(self._on_custom_path_changed)
        self.spin_workers.valueChanged.connect(self._on_workers_changed)
        self.spin_scan_depth.valueChanged.connect(self._on_scan_depth_changed)
        self.spin_min_size.valueChanged.connect(self._on_min_size_changed)
        self.combo_delete_source_mode.currentIndexChanged.connect(self._on_delete_source_mode_changed)
        self.cb_delete_source.toggled.connect(self._on_delete_source_toggled)
        self.cb_ad_filter.toggled.connect(self._on_ad_filter_toggled)
        self.edit_ad_patterns.editingFinished.connect(self._on_ad_patterns_finished)
        self.cb_watch_folder.toggled.connect(self._on_watch_folder_toggled)
        self.picker_watch_folder.path_changed.connect(self._on_watch_folder_path_changed)
        self.combo_theme.currentIndexChanged.connect(self._on_theme_combo_changed)
        self.cb_nested_limit_confirm.toggled.connect(self._on_nested_limit_confirm_toggled)
        self.spin_nested_confirm_limit.valueChanged.connect(self._on_nested_confirm_limit_changed)
        self.btn_clean_cache.clicked.connect(self._on_clean_cache_clicked)
        self.btn_reset_defaults.clicked.connect(self._on_reset_defaults_clicked)

        self._refresh_icons()

    def _refresh_icons(self):
        theme = get_current_theme()
        self.btn_clean_cache.setIcon(glyph_icon(ICON_DELETE, theme.danger, 16))
        self.btn_reset_defaults.setIcon(glyph_icon(ICON_REFRESH, theme.text2, 16))

    def _on_theme_changed(self, _mode: str):
        theme = get_current_theme()
        self._refresh_icons()
        for lbl in self.findChildren(SmoothLabel):
            lbl.set_text_color(theme.text)
        for picker in self.findChildren(PathPicker):
            picker._refresh_icon()
        repolish(self)
        self.update()

    # 兼容别名（旧代码/测试使用 edit_* 名称）
    @property
    def edit_bandizip_path(self):
        return self.picker_bandizip.line_edit

    @property
    def edit_winrar_path(self):
        return self.picker_winrar.line_edit

    @property
    def edit_sevenzip_path(self):
        return self.picker_sevenzip.line_edit

    @property
    def edit_custom_path(self):
        return self.picker_custom.line_edit

    @property
    def edit_watch_folder_path(self):
        return self.picker_watch_folder.line_edit

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

    def sync_delete_source_from_config(self):
        """静默同步源文件删除模式下拉框与兼容开关（不触发写盘）。"""
        del_mode = self._config.get("delete_source_mode")
        if del_mode not in ("keep", "recycle_bin", "permanent"):
            del_mode = "recycle_bin" if bool(self._config.get("delete_source_after_extract", False)) else "keep"

        self.combo_delete_source_mode.blockSignals(True)
        idx = self.combo_delete_source_mode.findData(del_mode)
        self.combo_delete_source_mode.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_delete_source_mode.blockSignals(False)

        self.cb_delete_source.blockSignals(True)
        self.cb_delete_source.setChecked(del_mode != "keep")
        self.cb_delete_source.blockSignals(False)

    def reload_from_config(self):
        """从配置回填所有控件（不触发写盘，绝不写注册表）。"""
        self._is_loading = True
        try:
            cfg = self._config.get_all()

            idx = self.combo_engine.findData(cfg.get("default_engine", "bandizip"))
            self.combo_engine.setCurrentIndex(idx if idx >= 0 else 0)

            mode = cfg.get("output_mode", "source_directory")
            out_idx = self.combo_output.findData(mode)
            self.combo_output.setCurrentIndex(out_idx if out_idx >= 0 else 0)

            bz_cfg = str(cfg.get("bandizip_path", "") or "").strip()
            if bz_cfg and os.path.basename(bz_cfg).lower() == "bandizip.exe":
                resolved_bz = resolve_bandizip_cli(bz_cfg)
                if resolved_bz:
                    bz_cfg = resolved_bz
            self.picker_bandizip.set_path(bz_cfg)
            self.picker_winrar.set_path(cfg.get("winrar_path", ""))
            self.picker_sevenzip.set_path(cfg.get("sevenzip_path", ""))
            self.picker_custom.set_path(cfg.get("custom_output_path", ""))
            self.row_bandizip.set_error("")
            self.row_winrar.set_error("")
            self.row_sevenzip.set_error("")
            self.row_custom.set_error("")

            self.spin_workers.setValue(int(cfg.get("max_workers", 2) or 2))
            self.spin_scan_depth.setValue(int(cfg.get("max_scan_depth", 4) or 4))
            min_size_mb = cfg.get("min_file_size_mb", 50)
            self.spin_min_size.setValue(int(50 if min_size_mb is None else min_size_mb))

            self.sync_delete_source_from_config()

            self.cb_ad_filter.setChecked(bool(cfg.get("enable_ad_filter", True)))
            raw_patterns = cfg.get("ad_filter_patterns", [])
            if isinstance(raw_patterns, list):
                patterns_str = ", ".join(str(p).strip() for p in raw_patterns if str(p).strip())
            else:
                patterns_str = str(raw_patterns or "")
            self.edit_ad_patterns.setText(patterns_str)
            self.edit_ad_patterns.setToolTip(patterns_str)

            self.cb_watch_folder.setChecked(bool(cfg.get("enable_watch_folder", False)))
            self.picker_watch_folder.set_path(cfg.get("watch_folder_path", ""))
            self.row_watch_folder_path.set_error("")

            theme_mode = str(cfg.get("theme", "light") or "light").strip().lower()
            theme_idx = self.combo_theme.findData("dark" if theme_mode in ("dark", "v2-dark") else "light")
            self.combo_theme.setCurrentIndex(theme_idx if theme_idx >= 0 else 0)

            self.cb_nested_limit_confirm.setChecked(bool(cfg.get("enable_nested_limit_confirm", True)))
            nested_limit = cfg.get("nested_confirm_limit", 10)
            self.spin_nested_confirm_limit.setValue(int(10 if nested_limit is None else nested_limit))

            self._sync_custom_state()
            self._sync_ad_filter_state()
            self._sync_watch_folder_state()
            self._sync_nested_limit_state()
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

    def _sync_ad_filter_state(self):
        enabled = self.cb_ad_filter.isChecked()
        self.row_ad_patterns.set_muted(not enabled)

    def _sync_watch_folder_state(self):
        enabled = self.cb_watch_folder.isChecked()
        self.row_watch_folder_path.set_muted(not enabled)
        if enabled:
            self.row_watch_folder_path.desc_label.setText("新增到该目录的压缩包将在稳定后自动解压")
        else:
            self.row_watch_folder_path.desc_label.setText("仅在开启“监听文件夹自动解压”后生效")

    def _sync_nested_limit_state(self):
        enabled = self.cb_nested_limit_confirm.isChecked()
        self.row_nested_confirm_limit.set_muted(not enabled)

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
        if path:
            if not os.path.exists(path):
                self.row_bandizip.set_error("路径无效：找不到该文件")
                self._set_status_invalid()
                return
            is_gui_or_dir = (
                os.path.basename(path).lower() == "bandizip.exe" or os.path.isdir(path)
            )
            resolved = resolve_bandizip_cli(path)
            if resolved is None:
                if is_gui_or_dir:
                    self.row_bandizip.set_error(
                        "不能使用图形界面程序 Bandizip.exe，请选择命令行程序 bz.exe"
                    )
                else:
                    self.row_bandizip.set_error(
                        "请选择 Bandizip 命令行程序 bz.exe（或 bc.exe）"
                    )
                self._set_status_invalid()
                return
            if is_gui_or_dir or resolved != path:
                self.picker_bandizip.set_path(resolved)
                self.row_bandizip.set_error("")
                self._commit("bandizip_path", resolved)
                if is_gui_or_dir:
                    self._set_status("已自动切换为命令行程序 bz.exe", "saved")
                    self._reset_status_timer.start()
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

    def _on_sevenzip_path_changed(self, path: str):
        if self._is_loading:
            return
        if path and not os.path.isfile(path):
            self.row_sevenzip.set_error("路径无效：找不到该文件")
            self._set_status_invalid()
            return
        self.row_sevenzip.set_error("")
        self._commit("sevenzip_path", path)

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

    def _on_delete_source_mode_changed(self, _index: int):
        if self._is_loading:
            return
        mode = self.combo_delete_source_mode.currentData() or "keep"
        self.cb_delete_source.blockSignals(True)
        self.cb_delete_source.setChecked(mode != "keep")
        self.cb_delete_source.blockSignals(False)
        self._commit("delete_source_mode", mode)

    def _on_delete_source_toggled(self, checked: bool):
        if self._is_loading:
            return
        current_mode = self.combo_delete_source_mode.currentData() or "keep"
        if checked:
            target_mode = current_mode if current_mode == "permanent" else "recycle_bin"
        else:
            target_mode = "keep"
        self.combo_delete_source_mode.blockSignals(True)
        idx = self.combo_delete_source_mode.findData(target_mode)
        self.combo_delete_source_mode.setCurrentIndex(idx if idx >= 0 else 0)
        self.combo_delete_source_mode.blockSignals(False)
        self._commit("delete_source_mode", target_mode)

    def _on_ad_filter_toggled(self, checked: bool):
        if self._is_loading:
            return
        self._sync_ad_filter_state()
        self._commit("enable_ad_filter", bool(checked))

    def _on_ad_patterns_finished(self):
        if self._is_loading:
            return
        patterns = parse_ad_filter_patterns(self.edit_ad_patterns.text())
        formatted = ", ".join(patterns)
        self.edit_ad_patterns.setText(formatted)
        self.edit_ad_patterns.setToolTip(formatted)
        self._commit("ad_filter_patterns", patterns)

    def _on_watch_folder_toggled(self, checked: bool):
        if self._is_loading:
            return
        self._sync_watch_folder_state()
        self._commit("enable_watch_folder", bool(checked))
        if checked:
            path = self.picker_watch_folder.path()
            if not path or not os.path.isdir(path):
                self.picker_watch_folder.btn_browse.setFocus()

    def _on_watch_folder_path_changed(self, path: str):
        if self._is_loading:
            return
        if path and not os.path.isdir(path):
            self.row_watch_folder_path.set_error("目录不存在")
            self._set_status_invalid()
            return
        self.row_watch_folder_path.set_error("")
        self._commit("watch_folder_path", path)

    def _on_theme_combo_changed(self, _index: int):
        if self._is_loading:
            return
        mode = self.combo_theme.currentData() or "light"
        self._commit("theme", mode)
        set_theme(mode)

    def _on_nested_limit_confirm_toggled(self, checked: bool):
        if self._is_loading:
            return
        self._sync_nested_limit_state()
        self._commit("enable_nested_limit_confirm", bool(checked))

    def _on_nested_confirm_limit_changed(self, value: int):
        if self._is_loading:
            return
        self._commit("nested_confirm_limit", int(value))

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
        set_theme(self._config.get("theme", "light"))
        self._set_status_reset()
        self.settingsChanged.emit("reset_defaults")

    # ------------------------------------------------------------- 兼容层
    def flush(self) -> bool:
        """写回全部控件当前值（无论是否 dirty）。校验失败时不写盘，不修改注册表。"""
        ok = True
        bz = self.picker_bandizip.path()
        if bz:
            if not os.path.exists(bz):
                self.row_bandizip.set_error("路径无效：找不到该文件")
                ok = False
            else:
                resolved_bz = resolve_bandizip_cli(bz)
                if resolved_bz is None:
                    if os.path.basename(bz).lower() == "bandizip.exe" or os.path.isdir(bz):
                        self.row_bandizip.set_error(
                            "不能使用图形界面程序 Bandizip.exe，请选择命令行程序 bz.exe"
                        )
                    else:
                        self.row_bandizip.set_error(
                            "请选择 Bandizip 命令行程序 bz.exe（或 bc.exe）"
                        )
                    ok = False
                else:
                    if resolved_bz != bz:
                        self.picker_bandizip.set_path(resolved_bz)
                        bz = resolved_bz
                    self.row_bandizip.set_error("")
        else:
            self.row_bandizip.set_error("")

        wr = self.picker_winrar.path()
        if wr and not os.path.isfile(wr):
            self.row_winrar.set_error("路径无效：找不到该文件")
            ok = False
        else:
            self.row_winrar.set_error("")

        sz = self.picker_sevenzip.path()
        if sz and not os.path.isfile(sz):
            self.row_sevenzip.set_error("路径无效：找不到该文件")
            ok = False
        else:
            self.row_sevenzip.set_error("")

        mode = self.combo_output.currentData() or "source_directory"
        custom = self.picker_custom.path()
        if mode == "custom" and (not custom or not os.path.isdir(custom)):
            self.row_custom.set_error("目录不存在，无法保存")
            ok = False
        else:
            self.row_custom.set_error("")

        watch_path = self.picker_watch_folder.path()
        if watch_path and not os.path.isdir(watch_path):
            self.row_watch_folder_path.set_error("目录不存在")
            ok = False
        else:
            self.row_watch_folder_path.set_error("")

        if not ok:
            self._set_status_invalid()
            return False

        prev_theme = str(self._config.get("theme", "light") or "light").strip().lower()
        del_mode = self.combo_delete_source_mode.currentData() or "keep"
        patterns = parse_ad_filter_patterns(self.edit_ad_patterns.text())
        theme_mode = self.combo_theme.currentData() or "light"

        self._set_status_saving()
        self._config.set("default_engine", self.combo_engine.currentData() or "bandizip")
        self._config.set("output_mode", mode)
        self._config.set("custom_output_path", custom)
        self._config.set("bandizip_path", bz)
        self._config.set("winrar_path", wr)
        self._config.set("sevenzip_path", sz)
        self._config.set("max_workers", int(self.spin_workers.value()))
        self._config.set("max_scan_depth", int(self.spin_scan_depth.value()))
        self._config.set("min_file_size_mb", int(self.spin_min_size.value()))
        self._config.set("delete_source_mode", del_mode)
        self._config.set("enable_ad_filter", bool(self.cb_ad_filter.isChecked()))
        self._config.set("ad_filter_patterns", patterns)
        self._config.set("enable_watch_folder", bool(self.cb_watch_folder.isChecked()))
        self._config.set("watch_folder_path", watch_path)
        self._config.set("theme", theme_mode)
        self._config.set("enable_nested_limit_confirm", bool(self.cb_nested_limit_confirm.isChecked()))
        self._config.set("nested_confirm_limit", int(self.spin_nested_confirm_limit.value()))
        self._config.save()

        if prev_theme != theme_mode:
            set_theme(theme_mode)

        self._set_status_saved()
        self.settingsChanged.emit("flush")
        return True
