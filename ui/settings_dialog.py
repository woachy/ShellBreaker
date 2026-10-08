"""
全局设置 QDialog — 阶段 6.1：手动指定引擎路径 + 浏览按钮。
设置项：解压引擎手动路径、默认输出路径、最大并发数、递归扫描、文件大小过滤。
所有设置实时持久化到 data/config.json。
"""
import os
import logging
from pathlib import Path

from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QGroupBox,
    QFormLayout,
    QLabel,
    QRadioButton,
    QButtonGroup,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QCheckBox,
    QFileDialog,
    QMessageBox,
    QDialogButtonBox,
    QScrollArea,
    QFrame,
    QWidget,
)
from PySide6.QtCore import Qt

from data.config_manager import ConfigManager

logger = logging.getLogger("ShellBreaker.SettingsDialog")


class SettingsDialog(QDialog):
    """全局设置独立面板。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙️ 全局设置")
        self.resize(580, 620)
        self.setMinimumSize(520, 560)

        self._config = ConfigManager()
        cfg = self._config.get_all()

        # ---------- 外层：可滚动内容区 + 底部固定按钮区 ----------
        outer = QVBoxLayout(self)
        outer.setSpacing(10)
        outer.setContentsMargins(10, 10, 10, 10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)

        content = QWidget()
        root = QVBoxLayout(content)
        root.setSpacing(10)
        root.setContentsMargins(4, 4, 4, 4)

        # ===== 引擎手动路径设置 =====
        engine_group = QGroupBox("🔧 解压引擎路径（手动指定优先于自动扫描）")
        engine_layout = QVBoxLayout(engine_group)

        # Bandizip 路径行
        bz_col = QVBoxLayout()
        bz_col.addWidget(QLabel("Bandizip (bz.exe):"))
        bz_row = QHBoxLayout()
        self._edit_bandizip_path = QLineEdit()
        self._edit_bandizip_path.setReadOnly(True)
        self._edit_bandizip_path.setPlaceholderText("未指定 — 将自动扫描")
        self._edit_bandizip_path.setText(cfg.get("bandizip_path", ""))
        bz_row.addWidget(self._edit_bandizip_path, stretch=1)
        btn_bz_browse = QPushButton("浏览...")
        btn_bz_browse.clicked.connect(self._on_browse_bandizip)
        bz_row.addWidget(btn_bz_browse)
        bz_col.addLayout(bz_row)
        engine_layout.addLayout(bz_col)

        # WinRAR 路径行
        wr_col = QVBoxLayout()
        wr_col.addWidget(QLabel("WinRAR.exe:"))
        wr_row = QHBoxLayout()
        self._edit_winrar_path = QLineEdit()
        self._edit_winrar_path.setReadOnly(True)
        self._edit_winrar_path.setPlaceholderText("未指定 — 将自动扫描")
        self._edit_winrar_path.setText(cfg.get("winrar_path", ""))
        wr_row.addWidget(self._edit_winrar_path, stretch=1)
        btn_wr_browse = QPushButton("浏览...")
        btn_wr_browse.clicked.connect(self._on_browse_winrar)
        wr_row.addWidget(btn_wr_browse)
        wr_col.addLayout(wr_row)
        engine_layout.addLayout(wr_col)

        root.addWidget(engine_group)

        # ===== 默认执行引擎 =====
        default_engine_group = QGroupBox("🧠 默认执行引擎")
        default_engine_layout = QVBoxLayout(default_engine_group)
        self._radio_default_bz = QRadioButton("优先使用 Bandizip (bz.exe)")
        self._radio_default_wr = QRadioButton("优先使用 WinRAR (WinRAR.exe)")
        self._default_engine_group = QButtonGroup(self)
        self._default_engine_group.addButton(self._radio_default_bz, 0)
        self._default_engine_group.addButton(self._radio_default_wr, 1)
        default_engine_layout.addWidget(self._radio_default_bz)
        default_engine_layout.addWidget(self._radio_default_wr)
        # 加载当前偏好
        default_engine = cfg.get("default_engine", "bandizip")
        if default_engine == "winrar":
            self._radio_default_wr.setChecked(True)
        else:
            self._radio_default_bz.setChecked(True)
        root.addWidget(default_engine_group)

        # ===== 输出路径 =====
        output_group = QGroupBox("📁 默认输出路径")
        output_layout = QVBoxLayout(output_group)

        self._radio_source = QRadioButton("解压到源文件同级目录")
        self._radio_custom = QRadioButton("自定义路径：")
        self._radio_group = QButtonGroup(self)
        self._radio_group.addButton(self._radio_source, 0)
        self._radio_group.addButton(self._radio_custom, 1)

        output_layout.addWidget(self._radio_source)

        custom_row = QHBoxLayout()
        custom_row.addWidget(self._radio_custom)
        self._edit_custom_path = QLineEdit()
        self._edit_custom_path.setPlaceholderText("选择或输入自定义输出路径...")
        self._edit_custom_path.setEnabled(False)
        custom_row.addWidget(self._edit_custom_path, stretch=1)

        self._btn_browse = QPushButton("浏览...")
        self._btn_browse.setEnabled(False)
        self._btn_browse.clicked.connect(self._on_browse)
        custom_row.addWidget(self._btn_browse)
        output_layout.addLayout(custom_row)

        # 根据当前配置设置选中项
        output_mode = cfg.get("output_mode", "source_directory")
        if output_mode == "custom":
            self._radio_custom.setChecked(True)
            self._edit_custom_path.setText(cfg.get("custom_output_path", ""))
            self._edit_custom_path.setEnabled(True)
            self._btn_browse.setEnabled(True)
        else:
            self._radio_source.setChecked(True)

        self._radio_source.toggled.connect(self._on_output_mode_changed)
        self._radio_custom.toggled.connect(self._on_output_mode_changed)
        root.addWidget(output_group)

        # ===== 性能与过滤（并发数 / 递归扫描 / 文件大小） =====
        perf_group = QGroupBox("⚡ 性能与过滤")
        perf_layout = QFormLayout(perf_group)

        self._spin_workers = QSpinBox()
        self._spin_workers.setMinimum(1)
        self._spin_workers.setMaximum(16)
        self._spin_workers.setValue(int(cfg.get("max_workers", 2)))
        self._spin_workers.setToolTip("同时解压的任务数量")
        perf_layout.addRow("并发数：", self._spin_workers)

        self._spin_scan_depth = QSpinBox()
        self._spin_scan_depth.setMinimum(1)
        self._spin_scan_depth.setMaximum(50)
        self._spin_scan_depth.setValue(int(cfg.get("max_scan_depth", 4)))
        self._spin_scan_depth.setToolTip("拖入文件夹时的最大遍历层数（1-50），超出层数直接忽略")
        perf_layout.addRow("最大扫描层数：", self._spin_scan_depth)

        self._spin_min_size_mb = QSpinBox()
        self._spin_min_size_mb.setMinimum(1)
        self._spin_min_size_mb.setMaximum(10000)
        self._spin_min_size_mb.setValue(int(cfg.get("min_file_size_mb", 50)))
        self._spin_min_size_mb.setToolTip("小于此大小的文件将被跳过，不进行解压（单位：MB）")
        self._spin_min_size_mb.setSuffix(" MB")
        perf_layout.addRow("最小解压文件大小：", self._spin_min_size_mb)

        self._cb_delete_source = QCheckBox("解压成功后自动删除源压缩包（移至回收站）")
        self._cb_delete_source.setToolTip("任务解压交付成功后，将源文件（包含同族分卷）安全移入系统回收站")
        self._cb_delete_source.setChecked(bool(cfg.get("delete_source_after_extract", False)))
        perf_layout.addRow("", self._cb_delete_source)

        root.addWidget(perf_group)

        root.addStretch(1)
        scroll.setWidget(content)
        outer.addWidget(scroll, stretch=1)

        # ===== 按钮区（固定在底部） =====
        button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        button_box.accepted.connect(self._on_save)
        button_box.rejected.connect(self.reject)
        outer.addWidget(button_box)

    # ---------- 槽函数 ----------

    def _on_browse_bandizip(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Bandizip CLI (bz.exe)", "", "可执行文件 (*.exe);;所有文件 (*)"
        )
        if path:
            self._edit_bandizip_path.setText(path)

    def _on_browse_winrar(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 WinRAR (WinRAR.exe)", "", "可执行文件 (*.exe);;所有文件 (*)"
        )
        if path:
            self._edit_winrar_path.setText(path)

    def _on_output_mode_changed(self, checked: bool):
        if not checked:
            return
        is_custom = self._radio_custom.isChecked()
        self._edit_custom_path.setEnabled(is_custom)
        self._btn_browse.setEnabled(is_custom)

    def _on_browse(self):
        path = QFileDialog.getExistingDirectory(self, "选择输出目录", self._edit_custom_path.text())
        if path:
            self._edit_custom_path.setText(path)

    def _on_save(self):
        # 验证自定义输出路径
        if self._radio_custom.isChecked():
            custom_path = self._edit_custom_path.text().strip()
            if not custom_path:
                QMessageBox.warning(self, "提示", "请填写或选择自定义输出路径")
                return
            if not Path(custom_path).exists():
                QMessageBox.warning(self, "提示", f"自定义路径不存在:\n{custom_path}")
                return
            self._config.set("custom_output_path", custom_path)
            self._config.set("output_mode", "custom")
        else:
            self._config.set("output_mode", "source_directory")
            self._config.set("custom_output_path", "")

        # 引擎路径
        bz_path = self._edit_bandizip_path.text().strip()
        wr_path = self._edit_winrar_path.text().strip()

        # 验证（若填写则路径必须存在）
        if bz_path and not os.path.isfile(bz_path):
            QMessageBox.warning(self, "提示", f"Bandizip 路径无效:\n{bz_path}")
            return
        if wr_path and not os.path.isfile(wr_path):
            QMessageBox.warning(self, "提示", f"WinRAR 路径无效:\n{wr_path}")
            return

        self._config.set("bandizip_path", bz_path)
        self._config.set("winrar_path", wr_path)

        # 默认引擎偏好
        default_engine = "winrar" if self._radio_default_wr.isChecked() else "bandizip"
        self._config.set("default_engine", default_engine)

        self._config.set("max_workers", self._spin_workers.value())
        self._config.set("max_scan_depth", self._spin_scan_depth.value())
        self._config.set("min_file_size_mb", self._spin_min_size_mb.value())
        self._config.set("delete_source_after_extract", self._cb_delete_source.isChecked())
        self._config.save()

        logger.info("全局设置已保存")
        self.accept()
