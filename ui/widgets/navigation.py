"""左侧导航 (NavigationView)。

260px 展开 / 48px 图标栏（窗口宽度 < 1008 时折叠）；
选中项左侧 3px 强调色竖条 + 淡强调底色。
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.theme.icons import (
    ICON_KEY,
    ICON_SETTINGS,
    ICON_TASK,
    app_icon_pixmap,
    glyph_icon,
)
from ui.theme.theme_manager import get_current_theme, repolish
from ui.theme.tokens import NAV_COLLAPSED_WIDTH, NAV_WIDTH


class NavItem(QPushButton):
    """导航条目：图标 + 文本，选中态由 QSS :checked 处理。"""

    def __init__(self, text: str, glyph: str, parent: QWidget | None = None):
        super().__init__(text, parent)
        self._glyph = glyph
        self._text = text
        self.setObjectName("NavItem")
        self.setCheckable(True)
        self.setFixedHeight(40)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(16, 16))
        # 不参与 Tab 焦点链，避免出现焦点描边破坏选中态视觉
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._apply_icon()

    def _apply_icon(self) -> None:
        theme = get_current_theme()
        color = theme.accent if self.isChecked() else theme.text2
        self.setIcon(glyph_icon(self._glyph, color, 16))

    def set_collapsed(self, collapsed: bool) -> None:
        self.setText("" if collapsed else self._text)
        self.setToolTip(self._text if collapsed else "")
        self.setAccessibleName(self._text)
        self.setProperty("collapsed", "true" if collapsed else "false")
        repolish(self)

    def setChecked(self, checked: bool):  # noqa: N802 - Qt 命名
        super().setChecked(checked)
        self._apply_icon()


class NavigationView(QFrame):
    """应用导航栏。"""

    page_changed = Signal(int)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Nav")
        self.setFixedWidth(NAV_WIDTH)
        self._collapsed = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 8, 0, 12)
        root.setSpacing(0)

        # ---- 品牌行 ----
        self.brand_row = QWidget(self)
        brand_lay = QHBoxLayout(self.brand_row)
        brand_lay.setContentsMargins(16, 8, 12, 14)
        brand_lay.setSpacing(8)

        self.app_icon_lbl = QLabel(self.brand_row)
        self.app_icon_lbl.setObjectName("AppIcon")
        self.app_icon_lbl.setFixedSize(20, 20)
        pixmap = app_icon_pixmap(20)
        if not pixmap.isNull():
            self.app_icon_lbl.setPixmap(pixmap)
        brand_lay.addWidget(self.app_icon_lbl, 0, Qt.AlignmentFlag.AlignVCenter)

        self.app_title_lbl = QLabel("便捷解压", self.brand_row)
        self.app_title_lbl.setObjectName("AppTitle")
        brand_lay.addWidget(self.app_title_lbl, 0, Qt.AlignmentFlag.AlignVCenter)
        brand_lay.addStretch(1)
        root.addWidget(self.brand_row)

        # ---- 工作区 ----
        self.lbl_workspace = self._make_section("工作区")
        root.addWidget(self.lbl_workspace)

        self.btn_tasks = NavItem("解压任务", ICON_TASK, self)
        self.btn_passwords = NavItem("密码库", ICON_KEY, self)
        root.addWidget(self.btn_tasks)
        root.addWidget(self.btn_passwords)

        root.addStretch(1)

        # ---- 设置（只有一项，不再显示“应用”分组标题） ----
        self.lbl_app = self._make_section("")
        self.lbl_app.setVisible(False)
        root.addWidget(self.lbl_app)

        self.btn_settings = NavItem("设置", ICON_SETTINGS, self)
        root.addWidget(self.btn_settings)

        self._buttons = [self.btn_tasks, self.btn_passwords, self.btn_settings]
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for index, button in enumerate(self._buttons):
            self._group.addButton(button, index)
            button.clicked.connect(lambda _=False, i=index: self._on_item_clicked(i))

        self._current = 0
        self.btn_tasks.setChecked(True)

    # ---- 构建辅助 ----
    def _make_section(self, text: str) -> QLabel:
        label = QLabel(text, self)
        label.setObjectName("NavSection")
        label.setContentsMargins(17, 6, 0, 6)
        return label

    # ---- 行为 ----
    def _on_item_clicked(self, index: int) -> None:
        self._current = index
        self.page_changed.emit(index)

    def set_current_index(self, index: int) -> None:
        if 0 <= index < len(self._buttons):
            self._current = index
            self._buttons[index].setChecked(True)
            for i, button in enumerate(self._buttons):
                if i != index:
                    button.setChecked(False)

    def current_index(self) -> int:
        return self._current

    def set_collapsed(self, collapsed: bool) -> None:
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        self.setFixedWidth(NAV_COLLAPSED_WIDTH if collapsed else NAV_WIDTH)
        self.app_title_lbl.setVisible(not collapsed)
        self.lbl_workspace.setVisible(not collapsed)
        # lbl_app（原“应用”分组标题）已停用，始终保持隐藏
        self.lbl_app.setVisible(False)
        self.brand_row.layout().setContentsMargins(
            14 if collapsed else 16, 8, 12 if not collapsed else 0, 14
        )
        for button in self._buttons:
            button.set_collapsed(collapsed)
        if collapsed:
            self.app_icon_lbl.setFixedSize(20, 20)
            self.brand_row.layout().setAlignment(self.app_icon_lbl, Qt.AlignmentFlag.AlignHCenter)
