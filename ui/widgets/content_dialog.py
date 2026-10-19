"""自绘对话框：通用内容框、密码输入框、密码导入预览。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtGui import QColor

from ui.theme.fonts import display_font
from ui.theme.icons import (
    ICON_EYE,
    ICON_EYE_HIDE,
    ICON_KEY,
    ICON_WARNING,
    glyph_icon,
    glyph_pixmap,
)
from ui.theme.theme_manager import ensure_theme, get_current_theme
from ui.widgets.smooth_label import SmoothLabel
from ui.theme.tokens import DIALOG_WIDTH


class ContentDialog(QDialog):
    """无边框圆角对话框：标题 + 说明 + 自定义内容 + 右下角按钮。"""

    def __init__(
        self,
        title: str,
        message: str = "",
        width: int = DIALOG_WIDTH,
        parent: QWidget | None = None,
        confirm_text: str = "确定",
        cancel_text: str | None = "取消",
    ):
        super().__init__(parent)
        ensure_theme(self)
        self.setModal(True)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMinimumWidth(width)
        self.resize(width, 200)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.setSpacing(0)

        self.card = QFrame(self)
        self.card.setObjectName("DialogCard")
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 8)
        shadow.setColor(QColor(0, 0, 0, 70))
        self.card.setGraphicsEffect(shadow)
        outer.addWidget(self.card)

        card = QVBoxLayout(self.card)
        card.setContentsMargins(24, 20, 24, 20)
        card.setSpacing(12)

        self.title_label = SmoothLabel(title, self.card, get_current_theme().text)
        self.title_label.setObjectName("DialogTitle")
        self.title_label.setFont(display_font(20))
        self.title_label.setWordWrap(True)
        card.addWidget(self.title_label)

        self.message_label = QLabel(message, self.card)
        self.message_label.setObjectName("DialogMessage")
        self.message_label.setWordWrap(True)
        self.message_label.setVisible(bool(message))
        card.addWidget(self.message_label)

        self.body_layout = QVBoxLayout()
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(8)
        card.addLayout(self.body_layout)

        card.addSpacing(4)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 0, 0, 0)
        btn_row.setSpacing(8)
        btn_row.addStretch(1)

        self.btn_cancel = QPushButton(cancel_text or "取消", self.card)
        self.btn_cancel.setObjectName("Secondary")
        self.btn_cancel.setMinimumWidth(88)
        self.btn_cancel.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_cancel.setVisible(bool(cancel_text))
        btn_row.addWidget(self.btn_cancel)

        self.btn_confirm = QPushButton(confirm_text, self.card)
        self.btn_confirm.setObjectName("Primary")
        self.btn_confirm.setMinimumWidth(88)
        self.btn_confirm.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_confirm.setDefault(True)
        btn_row.addWidget(self.btn_confirm)
        card.addLayout(btn_row)

        self.btn_cancel.clicked.connect(self.reject)
        self.btn_confirm.clicked.connect(self.accept)

    def keyPressEvent(self, event: QKeyEvent):  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)


class PasswordInputDialog(ContentDialog):
    """解压密码输入框。"""

    def __init__(self, archive_name: str, is_retry: bool = False, parent: QWidget | None = None):
        if is_retry:
            title = "密码错误"
            message = f"「{archive_name}」的密码不正确，请重新输入。"
            confirm_text = "重试"
        else:
            title = "需要密码"
            message = f"「{archive_name}」已加密，请输入解压密码。"
            confirm_text = "解锁"

        super().__init__(title, message, width=DIALOG_WIDTH, parent=parent, confirm_text=confirm_text)
        self._archive_name = archive_name
        self._is_retry = is_retry
        self._show_plain = False

        pwd_row = QHBoxLayout()
        pwd_row.setContentsMargins(0, 0, 0, 0)
        pwd_row.setSpacing(8)

        self.pwd_edit = QLineEdit(self.card)
        self.pwd_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.pwd_edit.setPlaceholderText("输入密码")
        self.pwd_edit.setMinimumHeight(32)
        pwd_row.addWidget(self.pwd_edit, 1)

        self.btn_toggle_eye = QPushButton(self.card)
        self.btn_toggle_eye.setObjectName("IconButton")
        self.btn_toggle_eye.setCheckable(True)
        self.btn_toggle_eye.setFixedSize(32, 32)
        self.btn_toggle_eye.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_toggle_eye.setToolTip("显示密码")
        pwd_row.addWidget(self.btn_toggle_eye)
        self.body_layout.addLayout(pwd_row)

        self.error_lbl = QLabel("", self.card)
        self.error_lbl.setObjectName("ErrorText")
        self.error_lbl.setVisible(False)
        self.body_layout.addWidget(self.error_lbl)

        self.btn_toggle_eye.toggled.connect(self._on_toggle_eye)
        self._refresh_icons()

    def _on_toggle_eye(self, checked: bool):
        self._show_plain = checked
        self.pwd_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )
        self.btn_toggle_eye.setToolTip("隐藏密码" if checked else "显示密码")
        self._refresh_icons()

    def _refresh_icons(self):
        theme = get_current_theme()
        if hasattr(self, "btn_toggle_eye"):
            glyph = ICON_EYE_HIDE if self._show_plain else ICON_EYE
            self.btn_toggle_eye.setIcon(glyph_icon(glyph, theme.text2, 16))

    def get_password(self) -> str:
        return self.pwd_edit.text().strip()

    def accept(self):  # noqa: N802
        if not self.get_password():
            self.error_lbl.setText("请输入密码")
            self.error_lbl.setVisible(True)
            self.pwd_edit.setProperty("error", "true")
            self.pwd_edit.style().unpolish(self.pwd_edit)
            self.pwd_edit.style().polish(self.pwd_edit)
            return
        super().accept()

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        self._refresh_icons()
        self.pwd_edit.setFocus()
        if self._is_retry:
            self.error_lbl.setText("上次输入的密码不正确")
            self.error_lbl.setVisible(True)


class ImportPreviewDialog(ContentDialog):
    """密码导入预览：显示前 5 条候选，确认后全部导入。"""

    PREVIEW_LIMIT = 5

    def __init__(self, candidates: list[str], source_name: str = "", parent: QWidget | None = None):
        self._candidates = list(candidates)
        count = len(self._candidates)
        preview_n = min(self.PREVIEW_LIMIT, count)
        message = (
            f"从 {source_name} 解析到 {count} 条候选密码。"
            f"确认后将全部导入，以下预览前 {preview_n} 条。"
        )
        super().__init__("导入密码", message, width=480, parent=parent, confirm_text="导入")

        preview = "\n".join(
            f"{i + 1}. {pwd}" for i, pwd in enumerate(self._candidates[: self.PREVIEW_LIMIT])
        )
        if count > self.PREVIEW_LIMIT:
            preview += f"\n… 另有 {count - self.PREVIEW_LIMIT} 条"

        self.preview_label = QLabel(preview, self.card)
        self.preview_label.setObjectName("MonoText")
        self.preview_label.setWordWrap(True)
        self.preview_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body_layout.addWidget(self.preview_label)

        if count == 0:
            self.btn_confirm.setEnabled(False)
            self.preview_label.setText("没有解析到可用的密码。")

    def result_passwords(self) -> list[str]:
        return list(self._candidates)
