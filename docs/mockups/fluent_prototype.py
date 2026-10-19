"""ShellBreaker Fluent UI design prototype (planning artifact, not production code).

Renders static mockups of the proposed Microsoft Fluent / PowerToys / Magpie
style shell. It uses fake data only and never imports the application's core,
config, task queue, or password store.

Run:
    QT_SCALE_FACTOR=2 python docs/mockups/fluent_prototype.py
Outputs:
    docs/mockups/out/*.png
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QLinearGradient,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "out"
ASSETS = Path(__file__).resolve().parent / "assets"
APP_ICON = ASSETS / "app_icon_v2.png"

# Segoe Fluent Icons glyphs (verified on the build machine).
ICON_TASK = "\uE8B7"       # ZipFolder
ICON_KEY = "\uE8D7"        # Permissions / key
ICON_SETTINGS = "\uE713"   # Settings
ICON_INFO = "\uE946"       # Info
ICON_FOLDER = "\uE8DA"     # Folder
ICON_FOLDER_OPEN = "\uE838"  # FolderOpen
ICON_FILE = "\uE8E5"       # OpenFile
ICON_ADD = "\uE710"        # Add
ICON_DELETE = "\uE74D"     # Delete
ICON_PASTE = "\uE77F"      # Paste
ICON_IMPORT = "\uE896"     # Download
ICON_MORE = "\uE712"       # More
ICON_CANCEL = "\uE711"     # Cancel
ICON_CHECK = "\uE73E"      # CheckMark
ICON_WARNING = "\uE7BA"    # Warning
ICON_MINIMIZE = "\uE921"
ICON_MAXIMIZE = "\uE922"
ICON_CLOSE = "\uE8BB"
ICON_CHEVRON_DOWN = "\uE70D"
ICON_CHEVRON_UP = "\uE70E"
ICON_REFRESH = "\uE72C"      # Refresh


@dataclass(frozen=True)
class Tokens:
    name: str
    window: str
    window_top: str
    window_bottom: str
    nav: str
    card: str
    card_border: str
    text: str
    text2: str
    text3: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_text: str
    accent_tint: str
    control: str
    control_border: str
    control_hover: str
    divider: str
    row_hover: str
    selection: str
    success: str
    success_bg: str
    danger: str
    danger_bg: str
    warning: str
    warning_bg: str
    neutral: str
    neutral_bg: str
    caption_hover: str
    shadow: str
    chevron: str


LIGHT = Tokens(
    name="light",
    window="#F3F3F3",
    window_top="#F3F3F3",
    window_bottom="#F3F3F3",
    nav="#EEEEEE",
    card="#FFFFFF",
    card_border="#E3E3E3",
    text="#1A1A1A",
    text2="#5D5D5D",
    text3="#8A8A8A",
    accent="#005FB8",
    accent_hover="#1A6FC4",
    accent_pressed="#004C94",
    accent_text="#FFFFFF",
    accent_tint="#EAF3FB",
    control="#FFFFFF",
    control_border="#D1D1D1",
    control_hover="#F9F9F9",
    divider="#EDEDED",
    row_hover="#FAFAFA",
    selection="#EFF6FC",
    success="#0F7B0F",
    success_bg="#DFF6DD",
    danger="#C42B1C",
    danger_bg="#FDE7E9",
    warning="#9D5D00",
    warning_bg="#FFF4CE",
    neutral="#5D5D5D",
    neutral_bg="#F0F0F0",
    caption_hover="#E5E5E5",
    shadow="#24000000",
    chevron="#5D5D5D",
)

DARK = Tokens(
    name="dark",
    window="#202020",
    window_top="#202020",
    window_bottom="#202020",
    nav="#1C1C1C",
    card="#2B2B2B",
    card_border="#3D3D3D",
    text="#FFFFFF",
    text2="#C5C5C5",
    text3="#8B8B8B",
    accent="#60CDFF",
    accent_hover="#79D5FF",
    accent_pressed="#3AA9DB",
    accent_text="#000000",
    accent_tint="#1B3A4B",
    control="#2D2D2D",
    control_border="#4A4A4A",
    control_hover="#333333",
    divider="#383838",
    row_hover="#2F2F2F",
    selection="#2A3F4F",
    success="#6CCB5F",
    success_bg="#1E3A1E",
    danger="#FF99A4",
    danger_bg="#442726",
    warning="#FCE100",
    warning_bg="#433519",
    neutral="#C5C5C5",
    neutral_bg="#333333",
    caption_hover="#333333",
    shadow="#66000000",
    chevron="#C5C5C5",
)

ORANGE_LIGHT_ACCENT = {
    "accent": "#C2410C",
    "accent_hover": "#D4551F",
    "accent_pressed": "#9A3412",
    "accent_text": "#FFFFFF",
    "accent_tint": "#FBEAE3",
    "selection": "#FDF2EC",
}

TEAL_LIGHT_ACCENT = {
    "accent": "#1D6978",
    "accent_hover": "#2A7F91",
    "accent_pressed": "#15525E",
    "accent_text": "#FFFFFF",
    "accent_tint": "#E7F1F3",
    "selection": "#EDF6F7",
}

# V2: PowerToys-adjacent blue-green accent + a very light blue page gradient.
V2_LIGHT = replace(
    LIGHT,
    name="v2-light",
    window="#F4F9FD",
    window_top="#FAFDFF",
    window_bottom="#E8F1FA",
    nav="#F3F9FD",
    card="#FFFFFF",
    card_border="#DCE7F1",
    text="#16212B",
    text2="#52616F",
    text3="#7E8D9A",
    accent="#0E7490",
    accent_hover="#1483A0",
    accent_pressed="#0A5A70",
    accent_tint="#E6F4F8",
    control="#FFFFFF",
    control_border="#C9D8E5",
    control_hover="#F5FAFD",
    divider="#E7EFF6",
    row_hover="#F7FBFE",
    selection="#EAF5F9",
    caption_hover="#E3EFF6",
    chevron="#52616F",
    shadow="#260B2A3A",
)

V2_DARK = replace(
    DARK,
    name="v2-dark",
    window="#1E2429",
    window_top="#242C32",
    window_bottom="#191F24",
    nav="#1B2228",
    card="#262E34",
    card_border="#3A464F",
    text="#F4F8FA",
    text2="#C2CED6",
    text3="#8A99A4",
    accent="#5BC8D8",
    accent_hover="#7AD8E5",
    accent_pressed="#3AA7B8",
    accent_text="#08181C",
    accent_tint="#14343B",
    control="#2B343B",
    control_border="#46535D",
    control_hover="#333D45",
    divider="#39444C",
    row_hover="#2C363D",
    selection="#1D3A42",
    caption_hover="#333D45",
    chevron="#C2CED6",
    shadow="#66000000",
)

STATUS_ROWS = [
    ("旅行照片合集.zip", "成功", "Bandizip (bz.exe)", r"E:\Downloads\旅行照片合集"),
    ("学习资料.rar", "正在提取 42%", "WinRAR", "正在解压第 2 层，已用时 00:18"),
    ("伪装视频.mp4", "排队中", "", "等待空闲解压槽位"),
    ("损坏的备份.7z", "失败", "Bandizip (bz.exe)", "CRC 校验失败，压缩包可能已损坏"),
    ("手机应用.apk", "已跳过", "", "受保护终点程序 (.apk)"),
    ("老照片合集.z01", "已跳过", "", "非首卷分卷（由首卷统一解压）"),
]

PASSWORD_ROWS = [
    ("x7Kp2mQz", "37", "[自动学习]"),
    ("archive2024", "22", "[自动学习]"),
    ("banzip!88", "15", "[剪贴板导入]"),
    ("f3R9vL0a", "9", "[自动学习]"),
    ("old-backup", "4", "[文件导入]"),
    ("manual-key-1", "1", "[手动入库]"),
]


def _font(size: int, weight: QFont.Weight = QFont.Weight.Normal, family: str = "") -> QFont:
    f = QFont(family or "Segoe UI Variable Text")
    f.setPixelSize(size)
    f.setWeight(weight)
    return f


def _glyph_font(size: int) -> QFont:
    f = QFont("Segoe Fluent Icons")
    f.setPixelSize(size)
    return f


def _glyph_pixmap(glyph: str, color: str, size: int = 16) -> QPixmap:
    """Render a Fluent glyph into a HiDPI pixmap (avoids QSS font conflicts)."""
    pix = QPixmap(size * 2, size * 2)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor(color))
    p.setFont(_glyph_font(size * 2))
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    p.end()
    pix.setDevicePixelRatio(2)
    return pix


def _glyph_icon(glyph: str, color: str, size: int = 16) -> QIcon:
    return QIcon(_glyph_pixmap(glyph, color, size))


def _with_glyph(button: QPushButton, glyph: str, color: str) -> QPushButton:
    button.setIcon(_glyph_icon(glyph, color, 16))
    button.setIconSize(QSize(16, 16))
    return button


def _make_chevron(t: Tokens) -> Path:
    ASSETS.mkdir(parents=True, exist_ok=True)
    path = ASSETS / f"chevron_{t.name}.png"
    if path.exists():
        return path
    pix = QPixmap(20, 20)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor(t.chevron))
    p.setFont(_glyph_font(10))
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, ICON_CHEVRON_DOWN)
    p.end()
    pix.save(str(path))
    return path


def build_qss(t: Tokens) -> str:
    chevron = _make_chevron(t).as_posix()
    qss = """
    * {
        color: @text;
        font-family: "Segoe UI Variable Text";
        font-size: 13px;
        outline: none;
    }
    QWidget#Root, QWidget#Content, QWidget#Page, QStackedWidget {
        background: transparent;
    }
    QScrollArea, QScrollArea > QWidget > QWidget {
        background: transparent;
    }
    QFrame#Nav {
        background: @nav;
        border-right: 1px solid @card_border;
    }
    QLabel#NavSection {
        color: @text3;
        font-size: 11px;
        font-weight: 600;
        padding: 0 16px;
    }
    QLabel#AppTitle {
        color: @text2;
        font-size: 12px;
    }
    QPushButton#Caption, QPushButton#CaptionClose {
        border: none;
        background: transparent;
        color: @text;
        font-family: "Segoe Fluent Icons";
        font-size: 10px;
        min-height: 32px;
        max-height: 32px;
        min-width: 46px;
        max-width: 46px;
        padding: 0;
    }
    QPushButton#Caption:hover, QPushButton#CaptionClose:hover { background: @caption_hover; }
    QPushButton#CaptionClose:hover { background: #C42B1C; color: #FFFFFF; }
    QLabel#PageTitle {
        font-size: 28px;
        font-weight: 600;
        color: @text;
    }
    QLabel#PageSubtitle {
        font-size: 13px;
        color: @text2;
    }
    QLabel#SectionTitle {
        font-size: 14px;
        font-weight: 600;
        color: @text;
    }
    QLabel#SectionDesc, QLabel#CardDesc, QLabel#Caption {
        font-size: 12px;
        color: @text2;
    }
    QLabel#CardTitle {
        font-size: 13px;
        color: @text;
    }
    QLabel#CardTitle[muted="true"] { color: @text3; }
    QLabel#CardDesc[muted="true"] { color: @text3; }
    QFrame#Card {
        background: @card;
        border: 1px solid @card_border;
        border-radius: 4px;
    }
    QFrame#GroupCard {
        background: @card;
        border: 1px solid @card_border;
        border-radius: 4px;
    }
    QWidget#SettingRow {
        background: transparent;
    }
    QFrame#RowDivider {
        background: @divider;
        border: none;
        max-height: 1px;
    }
    QFrame#NumberBox {
        background: @control;
        border: 1px solid @control_border;
        border-radius: 4px;
    }
    QFrame#NumberBox:hover {
        border-color: #A9C2D2;
    }
    QLineEdit#NumberEdit {
        background: transparent;
        border: none;
        padding-left: 8px;
        font-size: 13px;
        color: @text;
    }
    QPushButton#StepButton {
        background: transparent;
        border: none;
        padding: 0;
        min-width: 20px;
        max-width: 20px;
        min-height: 14px;
        max-height: 14px;
        color: @text2;
        font-family: "Segoe Fluent Icons";
        font-size: 8px;
    }
    QPushButton#StepButton:hover {
        background: @control_hover;
        border-radius: 2px;
    }
    QLabel#SavedStatus {
        color: @text3;
        font-size: 12px;
    }
    QFrame#DropCard {
        background: @card;
        border: 1px dashed #C7C7C7;
        border-radius: 4px;
    }
    QFrame#DropCard[drag="true"] {
        background: @accent_tint;
        border: 2px solid @accent;
    }
    QLabel#DropIconBox {
        background: @accent_tint;
        border-radius: 8px;
        color: @accent;
        font-family: "Segoe Fluent Icons";
        font-size: 22px;
    }
    QLabel#DropTitle {
        font-size: 16px;
        font-weight: 600;
        color: @text;
    }
    QLabel#DropSubtitle {
        font-size: 12px;
        color: @text2;
    }
    QPushButton {
        background: @control;
        border: 1px solid @control_border;
        border-radius: 4px;
        padding: 0 12px;
        min-height: 32px;
        max-height: 32px;
        font-size: 13px;
        color: @text;
    }
    QPushButton:hover { background: @control_hover; border-color: #B8B8B8; }
    QPushButton:pressed { background: @divider; }
    QPushButton:disabled { color: @text3; background: @control; border-color: @divider; }
    QPushButton#Primary {
        background: @accent;
        border: 1px solid @accent;
        color: @accent_text;
        font-weight: 600;
    }
    QPushButton#Primary:hover { background: @accent_hover; border-color: @accent_hover; }
    QPushButton#Primary:pressed { background: @accent_pressed; border-color: @accent_pressed; }
    QPushButton#Subtle {
        background: transparent;
        border: 1px solid transparent;
        color: @text;
    }
    QPushButton#Subtle:hover { background: @control_hover; border-color: transparent; }
    QPushButton#Danger { color: @danger; }
    QPushButton#Danger:hover { background: @danger_bg; border-color: transparent; }
    QPushButton#Subtle[danger="true"] { color: @danger; }
    QPushButton#Subtle[danger="true"]:hover { background: @danger_bg; }
    QPushButton#IconButton {
        min-width: 32px;
        max-width: 32px;
        padding: 0;
        background: transparent;
        border: 1px solid transparent;
        font-family: "Segoe Fluent Icons";
        font-size: 14px;
        color: @text2;
    }
    QPushButton#IconButton:hover { background: @control_hover; border-color: transparent; }
    QLabel#Badge {
        background: @neutral_bg;
        color: @text2;
        border-radius: 9px;
        min-width: 18px;
        max-height: 18px;
        font-size: 11px;
        font-weight: 600;
    }
    QLineEdit {
        background: @control;
        border: 1px solid @control_border;
        border-radius: 4px;
        padding: 0 9px;
        min-height: 32px;
        max-height: 32px;
        selection-background-color: @accent;
        selection-color: @accent_text;
    }
    QLineEdit:hover { border-color: #B8B8B8; }
    QLineEdit:focus { border: 1px solid @accent; }
    QLineEdit:read-only { background: @control_hover; color: @text2; }
    QLineEdit:disabled { background: @control; color: @text3; border-color: @divider; }
    QLineEdit::placeholder { color: @text3; }
    QComboBox {
        background: @control;
        border: 1px solid @control_border;
        border-radius: 4px;
        padding: 0 32px 0 9px;
        min-height: 32px;
        max-height: 32px;
    }
    QComboBox:hover { border-color: #B8B8B8; }
    QComboBox::drop-down {
        subcontrol-origin: padding;
        subcontrol-position: top right;
        width: 28px;
        border: none;
        background: transparent;
    }
    QComboBox::down-arrow {
        image: url("@chevron");
        width: 12px;
        height: 12px;
    }
    QComboBox QAbstractItemView {
        background: @card;
        border: 1px solid @card_border;
        border-radius: 4px;
        padding: 4px;
        selection-background-color: @control_hover;
        selection-color: @text;
        outline: none;
    }
    QSpinBox {
        background: @control;
        border: 1px solid @control_border;
        border-radius: 4px;
        padding: 0 8px;
        min-height: 32px;
        max-height: 32px;
    }
    QSpinBox:hover { border-color: #B8B8B8; }
    QSpinBox::up-button, QSpinBox::down-button { width: 0; border: none; }
    QTableWidget {
        background: transparent;
        border: none;
        gridline-color: transparent;
        selection-background-color: @selection;
        selection-color: @text;
        font-size: 13px;
    }
    QTableWidget::item {
        border-bottom: 1px solid @divider;
        padding-left: 12px;
        padding-right: 12px;
    }
    QTableWidget::item:selected {
        background: @selection;
        color: @text;
    }
    QTableWidget::item:hover { background: @row_hover; }
    QHeaderView::section {
        background: @control_hover;
        color: @text2;
        border: none;
        border-bottom: 1px solid @divider;
        padding-left: 12px;
        padding-right: 12px;
        font-size: 11px;
        font-weight: 600;
        min-height: 36px;
        max-height: 36px;
    }
    QScrollBar:vertical {
        background: transparent;
        width: 12px;
        margin: 2px;
    }
    QScrollBar::handle:vertical {
        background: #C7C7C7;
        border-radius: 4px;
        min-height: 28px;
    }
    QScrollBar::handle:vertical:hover { background: #A8A8A8; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
    QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
    QScrollBar:horizontal {
        background: transparent;
        height: 12px;
        margin: 2px;
    }
    QScrollBar::handle:horizontal {
        background: #C7C7C7;
        border-radius: 4px;
        min-width: 28px;
    }
    QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
    QFrame#StatusBar {
        background: transparent;
        border-top: 1px solid @card_border;
    }
    QLabel#StatusText { font-size: 12px; color: @text2; }
    QFrame#Footer {
        background: @window;
        border-top: 1px solid @card_border;
    }
    QFrame#DialogCard {
        background: @card;
        border: 1px solid @card_border;
        border-radius: 8px;
    }
    QLabel#DialogTitle { font-size: 20px; font-weight: 600; color: @text; }
    QLabel#DialogBody { font-size: 13px; color: @text2; }
    QLabel#ErrorText { font-size: 12px; color: @danger; }
    QFrame#ImportPreview {
        background: @control_hover;
        border: 1px solid @divider;
        border-radius: 4px;
    }
    QLabel#ImportRow { font-family: "Cascadia Mono"; font-size: 12px; color: @text; }
    QMenu {
        background: @card;
        border: 1px solid @card_border;
        border-radius: 8px;
        padding: 4px;
    }
    QMenu::item { padding: 7px 28px 7px 12px; border-radius: 4px; }
    QMenu::item:selected { background: @control_hover; }
    QToolTip {
        background: @text;
        color: @card;
        border: none;
        border-radius: 4px;
        padding: 5px 8px;
    }
    """
    replacements = {
        "@window": t.window,
        "@nav": t.nav,
        "@card": t.card,
        "@card_border": t.card_border,
        "@text3": t.text3,
        "@text2": t.text2,
        "@text": t.text,
        "@accent_tint": t.accent_tint,
        "@accent_hover": t.accent_hover,
        "@accent_pressed": t.accent_pressed,
        "@accent_text": t.accent_text,
        "@accent": t.accent,
        "@control_hover": t.control_hover,
        "@control_border": t.control_border,
        "@control": t.control,
        "@selection": t.selection,
        "@row_hover": t.row_hover,
        "@divider": t.divider,
        "@neutral_bg": t.neutral_bg,
        "@success_bg": t.success_bg,
        "@success": t.success,
        "@danger_bg": t.danger_bg,
        "@danger": t.danger,
        "@warning_bg": t.warning_bg,
        "@warning": t.warning,
        "@caption_hover": t.caption_hover,
        "@chevron": chevron,
    }
    for key in sorted(replacements, key=len, reverse=True):
        qss = qss.replace(key, replacements[key])
    return qss


class NavItem(QPushButton):
    def __init__(self, glyph: str, text: str, tokens: Tokens, parent=None):
        super().__init__(parent)
        self.glyph = glyph
        self.text = text
        self.t = tokens
        self._hover = False
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(40)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip(text)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self.t
        rect = QRectF(self.rect()).adjusted(4, 0, -4, 0)
        if self.isChecked():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.accent_tint))
            p.drawRoundedRect(rect, 4, 4)
        elif self._hover:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.control_hover))
            p.drawRoundedRect(rect, 4, 4)
        if self.isChecked():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.accent))
            p.drawRoundedRect(QRectF(0, 10, 3, 20), 1.5, 1.5)
        p.setFont(_glyph_font(16))
        p.setPen(QColor(t.accent if self.isChecked() else t.text2))
        p.drawText(QRectF(16, 0, 18, 40), Qt.AlignmentFlag.AlignCenter, self.glyph)
        p.setFont(_font(14, QFont.Weight.DemiBold if self.isChecked() else QFont.Weight.Normal))
        p.setPen(QColor(t.text if self.isChecked() else t.text2))
        p.drawText(QRectF(44, 0, self.width() - 52, 40), Qt.AlignmentFlag.AlignVCenter, self.text)
        if self.hasFocus():
            p.setPen(QPen(QColor(t.text), 1.5))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 3, 3)
        p.end()


class CaptionButton(QPushButton):
    def __init__(self, glyph: str, name: str = "Caption", parent=None):
        super().__init__(glyph, parent)
        self.setObjectName(name)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.ArrowCursor)


class TitleBar(QWidget):
    def __init__(self, tokens: Tokens, title: str = "便捷解压", parent=None):
        super().__init__(parent)
        self.t = tokens
        self.setFixedHeight(40)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 0, 0)
        lay.setSpacing(8)
        icon = QLabel()
        pm = QPixmap(str(APP_ICON))
        if pm.isNull():
            pm = QPixmap(str(ROOT / "assets" / "app_icon.png"))
        if not pm.isNull():
            icon.setPixmap(
                pm.scaled(
                    16,
                    16,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        icon.setFixedSize(16, 16)
        lay.addWidget(icon)
        label = QLabel(title)
        label.setObjectName("AppTitle")
        lay.addWidget(label)
        lay.addStretch(1)
        self.btn_min = CaptionButton(ICON_MINIMIZE)
        self.btn_max = CaptionButton(ICON_MAXIMIZE)
        self.btn_close = CaptionButton(ICON_CLOSE, "CaptionClose")
        for b in (self.btn_min, self.btn_max, self.btn_close):
            lay.addWidget(b)


class ToggleSwitch(QCheckBox):
    def __init__(self, text: str, tokens: Tokens, parent=None):
        super().__init__(text, parent)
        self.t = tokens
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(24)
        self._update_size()

    def _update_size(self):
        fm = QFontMetrics(_font(12))
        self.setMinimumWidth(48 + fm.horizontalAdvance(self.text()) + 2)
        self.setMaximumWidth(48 + fm.horizontalAdvance(self.text()) + 2)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self.t
        track = QRectF(0, 2, 40, 20)
        if self.isChecked():
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.accent))
            p.drawRoundedRect(track, 10, 10)
            p.setBrush(QColor(t.accent_text))
            p.drawEllipse(QRectF(23, 5, 14, 14))
        else:
            p.setPen(QPen(QColor(t.text3), 1))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRoundedRect(track.adjusted(0.5, 0.5, -0.5, -0.5), 9.5, 9.5)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(t.text3))
            p.drawEllipse(QRectF(5, 7, 10, 10))
        p.setFont(_font(12))
        p.setPen(QColor(t.text2))
        p.drawText(QRectF(48, 0, self.width() - 48, 24), Qt.AlignmentFlag.AlignVCenter, self.text())
        p.end()

    def hitButton(self, pos):
        return self.rect().contains(pos)


class StatusDelegate(QStyledItemDelegate):
    TONES = {
        "成功": ("success",),
        "失败": ("danger",),
        "正在提取": ("info",),
        "验证密码": ("info",),
        "排队中": ("neutral",),
        "已跳过": ("warning",),
    }

    def __init__(self, tokens: Tokens, parent=None):
        super().__init__(parent)
        self.t = tokens

    def _tone(self, text: str) -> tuple[str, str]:
        t = self.t
        if text.startswith("成功"):
            return t.success, t.success_bg
        if text.startswith("失败"):
            return t.danger, t.danger_bg
        if text.startswith("正在") or text.startswith("验证"):
            return t.accent, t.accent_tint
        if text.startswith("排队"):
            return t.neutral, t.neutral_bg
        if text.startswith("已跳过"):
            return t.warning, t.warning_bg
        return t.neutral, t.neutral_bg

    def paint(self, painter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        t = self.t
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.fillRect(rect, QColor(t.selection if selected else t.card))
        painter.setPen(QColor(t.divider))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        fg, bg = self._tone(text)
        f = _font(11, QFont.Weight.DemiBold)
        fm = QFontMetrics(f)
        pill_w = min(fm.horizontalAdvance(text) + 16, rect.width() - 24)
        pill = QRectF(rect.left() + 12, rect.center().y() - 10, pill_w, 20)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(bg))
        painter.drawRoundedRect(pill, 10, 10)
        painter.setFont(f)
        painter.setPen(QColor(fg))
        painter.drawText(pill, Qt.AlignmentFlag.AlignCenter, text)
        match = re.search(r"(\d+)%", text)
        if match and text.startswith("正在"):
            pct = max(0, min(100, int(match.group(1))))
            track = QRectF(rect.left() + 12, rect.bottom() - 8, 92, 2)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(t.divider))
            painter.drawRoundedRect(track, 1, 1)
            painter.setBrush(QColor(t.accent))
            painter.drawRoundedRect(QRectF(track.left(), track.top(), track.width() * pct / 100, 2), 1, 1)
        painter.restore()


class TaskTable(QTableWidget):
    HEADERS = ["文件名", "处理状态", "使用引擎", "详细备注"]

    def __init__(self, tokens: Tokens, rows=None, parent=None):
        super().__init__(parent)
        self.t = tokens
        self.setColumnCount(4)
        self.setHorizontalHeaderLabels(self.HEADERS)
        for i in range(4):
            item = self.horizontalHeaderItem(i)
            item.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(44)
        self.horizontalHeader().setFixedHeight(36)
        self.horizontalHeader().setHighlightSections(False)
        self.horizontalHeader().setSectionsClickable(False)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        self.setWordWrap(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setMouseTracking(True)
        self.setItemDelegateForColumn(1, StatusDelegate(tokens, self))
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.setColumnWidth(1, 156)
        self.setColumnWidth(2, 132)
        self.setColumnWidth(0, 300)
        self.setColumnWidth(3, 260)
        self.setRows(rows or [])

    def setRows(self, rows):
        self.setRowCount(0)
        for filename, status, engine, remark in rows:
            r = self.rowCount()
            self.insertRow(r)
            self.setRowHeight(r, 44)
            name_item = QTableWidgetItem(filename)
            name_item.setToolTip(filename)
            self.setItem(r, 0, name_item)
            status_item = QTableWidgetItem(status)
            self.setItem(r, 1, status_item)
            engine_item = QTableWidgetItem(engine)
            engine_item.setForeground(QColor(self.t.text2))
            engine_item.setFont(_font(12))
            self.setItem(r, 2, engine_item)
            remark_item = QTableWidgetItem(remark)
            remark_item.setForeground(QColor(self.t.text2))
            remark_item.setFont(_font(12))
            remark_item.setToolTip(remark)
            self.setItem(r, 3, remark_item)


class DropCard(QFrame):
    def __init__(self, tokens: Tokens, parent=None):
        super().__init__(parent)
        self.setObjectName("DropCard")
        self.setFixedHeight(128)
        self.setProperty("drag", False)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(16)
        icon_box = QLabel()
        icon_box.setObjectName("DropIconBox")
        icon_box.setFixedSize(56, 56)
        icon_box.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_box.setPixmap(_glyph_pixmap(ICON_TASK, tokens.accent, 22))
        lay.addWidget(icon_box)
        text_box = QVBoxLayout()
        text_box.setSpacing(4)
        title = QLabel("拖入压缩包或文件夹")
        title.setObjectName("DropTitle")
        subtitle = QLabel("支持 ZIP / RAR / 7Z / TAR 与伪装后缀，自动递归解压")
        subtitle.setObjectName("DropSubtitle")
        text_box.addWidget(title)
        text_box.addWidget(subtitle)
        lay.addLayout(text_box, 1)
        self.btn_files = QPushButton("选择文件")
        self.btn_files.setObjectName("Primary")
        self.btn_files.setMinimumWidth(96)
        self.btn_dir = QPushButton("选择文件夹")
        self.btn_dir.setMinimumWidth(104)
        lay.addWidget(self.btn_files)
        lay.addWidget(self.btn_dir)


class CardHeader(QWidget):
    def __init__(
        self,
        tokens: Tokens,
        title: str,
        count: int | None = None,
        action_text: str | None = None,
        action_icon: str | None = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setFixedHeight(48)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 0, 12, 0)
        lay.setSpacing(8)
        label = QLabel(title)
        label.setObjectName("CardTitle")
        label.setFont(_font(13, QFont.Weight.DemiBold))
        lay.addWidget(label)
        if count is not None:
            badge = QLabel(str(count))
            badge.setObjectName("Badge")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setFixedSize(20, 18)
            lay.addWidget(badge)
        lay.addStretch(1)
        if action_text:
            action = QPushButton(action_text)
            action.setObjectName("Subtle")
            if action_icon:
                _with_glyph(action, action_icon, tokens.text2)
            action.setEnabled(bool(count))
            lay.addWidget(action)


class StatusBar(QFrame):
    def __init__(self, tokens: Tokens, counts=None, parent=None):
        super().__init__(parent)
        counts = counts or {"total": 0, "success": 0, "failed": 0, "processing": 0, "skipped": 0}
        self.setObjectName("StatusBar")
        self.setFixedHeight(36)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(40, 0, 24, 0)
        lay.setSpacing(10)
        stats = QLabel(
            f'<span style="color:{tokens.text2}">共 {counts["total"]} 个任务</span>'
            f'&nbsp;&nbsp;<span style="color:{tokens.success}">●</span>'
            f'<span style="color:{tokens.text2}"> 成功 {counts["success"]}</span>'
            f'&nbsp;&nbsp;<span style="color:{tokens.danger}">●</span>'
            f'<span style="color:{tokens.text2}"> 失败 {counts["failed"]}</span>'
            f'&nbsp;&nbsp;<span style="color:{tokens.accent}">●</span>'
            f'<span style="color:{tokens.text2}"> 处理中 {counts["processing"]}</span>'
            f'&nbsp;&nbsp;<span style="color:{tokens.warning}">●</span>'
            f'<span style="color:{tokens.text2}"> 已跳过 {counts["skipped"]}</span>'
        )
        stats.setObjectName("StatusText")
        stats.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(stats)
        lay.addStretch(1)
        self.toggle = ToggleSwitch("解压后移至回收站", tokens)
        lay.addWidget(self.toggle)


class EmptyState(QWidget):
    def __init__(self, tokens: Tokens, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 24, 24, 24)
        lay.setSpacing(6)
        lay.addStretch(1)
        icon = QLabel()
        icon.setPixmap(_glyph_pixmap(ICON_TASK, tokens.text3, 32))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(icon)
        title = QLabel("还没有解压任务")
        title.setObjectName("DropTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)
        subtitle = QLabel("将文件或文件夹拖到上方，或点击「选择文件」开始")
        subtitle.setObjectName("CardDesc")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(subtitle)
        lay.addStretch(1)


class TasksPage(QWidget):
    def __init__(self, tokens: Tokens, rows=None, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.t = tokens
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        content = QWidget()
        content.setObjectName("Content")
        lay = QVBoxLayout(content)
        lay.setContentsMargins(40, 24, 40, 16)
        lay.setSpacing(0)
        header = QVBoxLayout()
        header.setSpacing(3)
        title = QLabel("解压任务")
        title.setObjectName("PageTitle")
        subtitle = QLabel("拖入文件或文件夹，自动识别伪装并逐层解压")
        subtitle.setObjectName("PageSubtitle")
        header.addWidget(title)
        header.addWidget(subtitle)
        lay.addLayout(header)
        lay.addSpacing(20)
        self.drop = DropCard(tokens)
        lay.addWidget(self.drop)
        lay.addSpacing(16)
        self.card = QFrame()
        self.card.setObjectName("Card")
        card_lay = QVBoxLayout(self.card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)
        card_header = CardHeader(
            tokens,
            "任务",
            len(rows or []),
            action_text="清空已完成",
            action_icon=ICON_DELETE,
        )
        card_lay.addWidget(card_header)
        if rows:
            self.table = TaskTable(tokens, rows)
            card_lay.addWidget(self.table, 1)
        else:
            self.table = TaskTable(tokens, [])
            self.table.setVisible(False)
            card_lay.addWidget(EmptyState(tokens), 1)
        lay.addWidget(self.card, 1)
        outer.addWidget(content, 1)
        rows = rows or []
        success = sum(1 for row in rows if row[1].startswith("成功"))
        failed = sum(1 for row in rows if row[1].startswith("失败"))
        skipped = sum(1 for row in rows if row[1].startswith("已跳过"))
        counts = {
            "total": len(rows),
            "success": success,
            "failed": failed,
            "processing": len(rows) - success - failed - skipped,
            "skipped": skipped,
        }
        self.status = StatusBar(tokens, counts)
        outer.addWidget(self.status)


class SettingRow(QWidget):
    """One PowerToys-style row inside a grouped settings card."""

    def __init__(
        self,
        tokens: Tokens,
        glyph: str,
        title: str,
        desc: str,
        control: QWidget,
        parent=None,
        muted: bool = False,
    ):
        super().__init__(parent)
        self.setObjectName("SettingRow")
        self.setMinimumHeight(64)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 10, 16, 10)
        lay.setSpacing(12)
        icon = QLabel()
        icon.setFixedSize(22, 22)
        icon.setPixmap(_glyph_pixmap(glyph, tokens.text3 if muted else tokens.text2, 18))
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(icon)
        text = QVBoxLayout()
        text.setSpacing(2)
        title_label = QLabel(title)
        title_label.setObjectName("CardTitle")
        title_label.setProperty("muted", muted)
        desc_label = QLabel(desc)
        desc_label.setObjectName("CardDesc")
        desc_label.setProperty("muted", muted)
        desc_label.setWordWrap(True)
        text.addWidget(title_label)
        text.addWidget(desc_label)
        lay.addLayout(text, 1)
        control.setEnabled(not muted)
        lay.addWidget(control, 0, Qt.AlignmentFlag.AlignVCenter)


class SettingsGroup(QFrame):
    def __init__(self, tokens: Tokens, parent=None):
        super().__init__(parent)
        self.setObjectName("GroupCard")
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)

    def add_row(self, row: SettingRow, divider: bool = True) -> None:
        self._lay.addWidget(row)
        if divider:
            line = QFrame()
            line.setObjectName("RowDivider")
            line.setFixedHeight(1)
            self._lay.addWidget(line)


class NumberBox(QFrame):
    """Compact Fluent NumberBox with a chevron stepper."""

    def __init__(self, tokens: Tokens, value: int, suffix: str = "", width: int = 124, parent=None):
        super().__init__(parent)
        self.setObjectName("NumberBox")
        self.setFixedSize(width, 32)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 2, 0)
        lay.setSpacing(0)
        self.edit = QLineEdit(f"{value}{suffix}")
        self.edit.setObjectName("NumberEdit")
        self.edit.setReadOnly(True)
        self.edit.setFrame(False)
        self.edit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        lay.addWidget(self.edit, 1)
        stepper = QVBoxLayout()
        stepper.setContentsMargins(0, 1, 0, 1)
        stepper.setSpacing(0)
        for glyph in (ICON_CHEVRON_UP, ICON_CHEVRON_DOWN):
            btn = QPushButton(glyph)
            btn.setObjectName("StepButton")
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            stepper.addWidget(btn)
        lay.addLayout(stepper)


class PathControl(QWidget):
    def __init__(self, tokens: Tokens, value: str = "", placeholder: str = "未指定 — 将自动扫描", width: int = 286, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.edit = QLineEdit(value)
        self.edit.setReadOnly(True)
        self.edit.setPlaceholderText(placeholder)
        self.edit.setFixedWidth(width)
        self.btn_browse = QPushButton("浏览…")
        self.btn_clear = QPushButton(ICON_CANCEL)
        self.btn_clear.setObjectName("IconButton")
        self.btn_clear.setToolTip("清除，恢复自动扫描")
        lay.addWidget(self.edit)
        lay.addWidget(self.btn_browse)
        lay.addWidget(self.btn_clear)


class SettingsPage(QWidget):
    def __init__(self, tokens: Tokens, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.t = tokens
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        scroll = QScrollArea()
        self.scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body.setObjectName("Page")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(24, 24, 24, 32)
        lay.setSpacing(0)

        header = QHBoxLayout()
        header.setSpacing(16)
        heading = QVBoxLayout()
        heading.setSpacing(2)
        title = QLabel("设置")
        title.setObjectName("PageTitle")
        subtitle = QLabel("更改会立即保存；路径无效时不会写入配置。")
        subtitle.setObjectName("PageSubtitle")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header.addLayout(heading, 1)
        saved = QWidget()
        saved_lay = QHBoxLayout(saved)
        saved_lay.setContentsMargins(0, 8, 0, 0)
        saved_lay.setSpacing(6)
        check = QLabel()
        check.setPixmap(_glyph_pixmap(ICON_CHECK, tokens.accent, 14))
        check.setFixedSize(16, 16)
        saved_lay.addWidget(check)
        saved_label = QLabel("所有更改已保存")
        saved_label.setObjectName("SavedStatus")
        saved_lay.addWidget(saved_label)
        header.addWidget(saved, 0, Qt.AlignmentFlag.AlignTop)
        lay.addLayout(header)
        lay.addSpacing(20)

        def section(name: str, desc: str, first: bool = False) -> None:
            if not first:
                lay.addSpacing(24)
            section_title = QLabel(name)
            section_title.setObjectName("SectionTitle")
            section_desc = QLabel(desc)
            section_desc.setObjectName("SectionDesc")
            lay.addWidget(section_title)
            lay.addWidget(section_desc)
            lay.addSpacing(8)

        def group() -> SettingsGroup:
            widget = SettingsGroup(tokens)
            lay.addWidget(widget)
            return widget

        section("引擎", "优先使用手动指定的路径；留空时自动扫描常见安装位置。", first=True)
        engine_group = group()
        engine_combo = QComboBox()
        engine_combo.addItems(["Bandizip (bz.exe)", "WinRAR"])
        engine_combo.setFixedWidth(200)
        engine_group.add_row(
            SettingRow(
                tokens,
                ICON_SETTINGS,
                "默认执行引擎",
                "决定优先尝试哪个解压内核。",
                engine_combo,
            )
        )
        engine_group.add_row(
            SettingRow(
                tokens,
                ICON_FILE,
                "Bandizip (bz.exe)",
                "留空则自动扫描 Bandizip 的标准安装路径。",
                PathControl(tokens, width=270),
            )
        )
        engine_group.add_row(
            SettingRow(
                tokens,
                ICON_FILE,
                "WinRAR (WinRAR.exe)",
                "留空则自动扫描 WinRAR 的标准安装路径。",
                PathControl(tokens, width=270),
            ),
            divider=False,
        )

        section("输出", "所有任务的默认解压位置；自定义路径必须已存在。")
        output_group = group()
        output_combo = QComboBox()
        output_combo.addItems(["源文件同级目录", "自定义目录…"])
        output_combo.setFixedWidth(200)
        output_group.add_row(
            SettingRow(
                tokens,
                ICON_FOLDER_OPEN,
                "输出位置",
                "选择解压产物的存放方式。",
                output_combo,
            )
        )
        output_group.add_row(
            SettingRow(
                tokens,
                ICON_FOLDER,
                "自定义输出目录",
                "仅在选择「自定义目录」时生效。",
                PathControl(tokens, r"E:\Downloads\解压输出", "选择或输入自定义输出路径…", 270),
                muted=True,
            ),
            divider=False,
        )

        section("性能与过滤", "根据机器性能调整并发与扫描深度；过小的文件会被跳过。")
        performance_group = group()
        performance_group.add_row(
            SettingRow(
                tokens,
                ICON_SETTINGS,
                "并发任务数",
                "同时运行的最大解压任务数量（1–16）。",
                NumberBox(tokens, 2, " 个"),
            )
        )
        performance_group.add_row(
            SettingRow(
                tokens,
                ICON_FOLDER_OPEN,
                "最大扫描层数",
                "目录递归扫描的最大深度（1–50）。",
                NumberBox(tokens, 4, " 层"),
            )
        )
        performance_group.add_row(
            SettingRow(
                tokens,
                ICON_FILE,
                "最小解压文件大小",
                "小于该大小的文件将被跳过，避免解压零碎文件。",
                NumberBox(tokens, 50, " MB"),
            ),
            divider=False,
        )

        section("行为", "任务完成后的清理行为。")
        behavior_group = group()
        behavior_group.add_row(
            SettingRow(
                tokens,
                ICON_DELETE,
                "解压成功后移至回收站",
                "源压缩包及关联同族分卷将安全移入 Windows 回收站。",
                ToggleSwitch("", tokens),
            ),
            divider=False,
        )

        section("高级", "维护操作与重置。")
        advanced_group = group()
        clear_btn = QPushButton("清空缓存…")
        clear_btn.setObjectName("Subtle")
        clear_btn.setProperty("danger", True)
        advanced_group.add_row(
            SettingRow(
                tokens,
                ICON_DELETE,
                "临时文件",
                "删除所有沙箱临时目录，不影响已完成任务。",
                clear_btn,
            )
        )
        reset_btn = QPushButton("恢复默认")
        reset_btn.setObjectName("Subtle")
        advanced_group.add_row(
            SettingRow(
                tokens,
                ICON_REFRESH,
                "恢复默认设置",
                "仅重置引擎、输出、性能与行为选项；不删除密码库。",
                reset_btn,
            ),
            divider=False,
        )

        lay.addStretch(1)
        scroll.setWidget(body)
        outer.addWidget(scroll, 1)


class PasswordTable(QTableWidget):
    def __init__(self, tokens: Tokens, rows=None, parent=None):
        super().__init__(parent)
        self.t = tokens
        self.setColumnCount(3)
        self.setHorizontalHeaderLabels(["密码（明文）", "使用次数", "标签"])
        for i in range(3):
            item = self.horizontalHeaderItem(i)
            align = (
                Qt.AlignmentFlag.AlignCenter
                if i == 1
                else Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
            )
            item.setTextAlignment(align)
        self.verticalHeader().setVisible(False)
        self.verticalHeader().setDefaultSectionSize(44)
        self.horizontalHeader().setFixedHeight(36)
        self.horizontalHeader().setHighlightSections(False)
        self.horizontalHeader().setSectionsClickable(False)
        self.setShowGrid(False)
        self.setAlternatingRowColors(False)
        self.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(1, 120)
        self.setColumnWidth(2, 180)
        self.setRows(rows or [])

    def setRows(self, rows):
        self.setRowCount(0)
        for password, count, tag in rows:
            r = self.rowCount()
            self.insertRow(r)
            self.setRowHeight(r, 44)
            pwd = QTableWidgetItem(password)
            pwd.setFont(QFont("Cascadia Mono", 10))
            pwd.setForeground(QColor(self.t.text))
            self.setItem(r, 0, pwd)
            cnt = QTableWidgetItem(count)
            cnt.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            cnt.setForeground(QColor(self.t.text2))
            self.setItem(r, 1, cnt)
            tag_item = QTableWidgetItem(tag)
            tag_item.setForeground(QColor(self.t.text2))
            tag_item.setFont(_font(12))
            self.setItem(r, 2, tag_item)


class PasswordPage(QWidget):
    def __init__(self, tokens: Tokens, rows=None, parent=None):
        super().__init__(parent)
        self.setObjectName("Page")
        self.t = tokens
        outer = QVBoxLayout(self)
        outer.setContentsMargins(40, 24, 40, 24)
        outer.setSpacing(0)
        header = QHBoxLayout()
        header.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(3)
        title = QLabel("密码库")
        title.setObjectName("PageTitle")
        subtitle = QLabel(f"共 {len(rows or [])} 条密码 · 按使用频率排序")
        subtitle.setObjectName("PageSubtitle")
        text.addWidget(title)
        text.addWidget(subtitle)
        header.addLayout(text, 1)
        self.btn_add = QPushButton("添加密码")
        self.btn_add.setObjectName("Primary")
        self.btn_add.setMinimumWidth(112)
        _with_glyph(self.btn_add, ICON_ADD, tokens.accent_text)
        header.addWidget(self.btn_add, 0, Qt.AlignmentFlag.AlignVCenter)
        outer.addLayout(header)
        outer.addSpacing(20)
        self.card = QFrame()
        self.card.setObjectName("Card")
        card_lay = QVBoxLayout(self.card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)
        toolbar = QWidget()
        toolbar.setFixedHeight(48)
        t_lay = QHBoxLayout(toolbar)
        t_lay.setContentsMargins(16, 0, 12, 0)
        t_lay.setSpacing(6)
        label = QLabel("全部密码")
        label.setFont(_font(13, QFont.Weight.DemiBold))
        t_lay.addWidget(label)
        badge = QLabel(str(len(rows or [])))
        badge.setObjectName("Badge")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(20, 18)
        t_lay.addWidget(badge)
        t_lay.addStretch(1)
        self.btn_import_clip = QPushButton("从剪贴板导入")
        self.btn_import_clip.setObjectName("Subtle")
        _with_glyph(self.btn_import_clip, ICON_PASTE, tokens.text2)
        self.btn_import_file = QPushButton("从文件导入")
        self.btn_import_file.setObjectName("Subtle")
        _with_glyph(self.btn_import_file, ICON_IMPORT, tokens.text2)
        self.btn_delete = QPushButton("删除")
        self.btn_delete.setObjectName("Subtle")
        _with_glyph(self.btn_delete, ICON_DELETE, tokens.text2)
        self.btn_clear = QPushButton("清空")
        self.btn_clear.setObjectName("Subtle")
        self.btn_clear.setProperty("danger", True)
        _with_glyph(self.btn_clear, ICON_DELETE, tokens.danger)
        for btn in (
            self.btn_import_clip,
            self.btn_import_file,
            self.btn_delete,
            self.btn_clear,
        ):
            btn.setStyleSheet("padding: 0 8px;")
            t_lay.addWidget(btn)
        card_lay.addWidget(toolbar)
        self.table = PasswordTable(tokens, rows)
        card_lay.addWidget(self.table, 1)
        outer.addWidget(self.card, 1)


class DialogCard(QFrame):
    def __init__(self, tokens: Tokens, title: str, body: str, width: int = 440, parent=None):
        super().__init__(parent)
        self.setObjectName("DialogCard")
        self.setFixedWidth(width)
        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(40)
        shadow.setOffset(0, 10)
        shadow.setColor(QColor(0, 0, 0, 90))
        self.setGraphicsEffect(shadow)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(24, 22, 24, 20)
        self.lay.setSpacing(12)
        self.title = QLabel(title)
        self.title.setObjectName("DialogTitle")
        self.body = QLabel(body)
        self.body.setObjectName("DialogBody")
        self.body.setWordWrap(True)
        self.lay.addWidget(self.title)
        self.lay.addWidget(self.body)
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(8)
        self.buttons.addStretch(1)
        self.lay.addSpacing(4)
        self.lay.addLayout(self.buttons)

    def add_button(self, text: str, primary: bool = False, danger: bool = False) -> QPushButton:
        btn = QPushButton(text)
        if primary:
            btn.setObjectName("Primary")
        if danger:
            btn.setObjectName("Danger")
        btn.setMinimumWidth(88)
        btn.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self.buttons.addWidget(btn)
        return btn


class Overlay(QWidget):
    def __init__(self, parent: QWidget, card: DialogCard, tokens: Tokens):
        super().__init__(parent)
        self.t = tokens
        self.card = card
        self.card.setParent(self)
        self.setGeometry(parent.rect())
        self.card.adjustSize()
        self.card.layout().activate()
        self._center()
        self.raise_()
        self.card.raise_()

    def _center(self):
        cw = self.card.width()
        ch = self.card.sizeHint().height()
        self.card.setFixedHeight(ch)
        self.card.move((self.width() - cw) // 2, (self.height() - ch) // 2)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 76))
        p.end()

    def resizeEvent(self, event):
        self.setGeometry(self.parent().rect())
        self._center()
        super().resizeEvent(event)


class Shell(QWidget):
    def __init__(self, tokens: Tokens, page: str = "tasks", height: int = 760, parent=None):
        super().__init__(parent)
        self.t = tokens
        self.setObjectName("Root")
        self.setWindowTitle("便捷解压")
        self._overlay = None
        self.setFixedSize(1160, height) if not self.isWindow() else None
        self.resize(1160, height)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        nav = QFrame()
        nav.setObjectName("Nav")
        nav.setFixedWidth(260)
        nav_lay = QVBoxLayout(nav)
        nav_lay.setContentsMargins(0, 0, 0, 8)
        nav_lay.setSpacing(0)
        nav_lay.addSpacing(40)
        section = QLabel("工作区")
        section.setObjectName("NavSection")
        section.setFixedHeight(30)
        nav_lay.addWidget(section)
        self.nav_tasks = NavItem(ICON_TASK, "解压任务", tokens)
        self.nav_passwords = NavItem(ICON_KEY, "密码库", tokens)
        nav_lay.addWidget(self.nav_tasks)
        nav_lay.addWidget(self.nav_passwords)
        nav_lay.addStretch(1)
        section2 = QLabel("应用")
        section2.setObjectName("NavSection")
        section2.setFixedHeight(30)
        nav_lay.addWidget(section2)
        self.nav_settings = NavItem(ICON_SETTINGS, "设置", tokens)
        nav_lay.addWidget(self.nav_settings)
        lay.addWidget(nav)
        self.stack = QStackedWidget()
        self.stack.setObjectName("Content")
        self.tasks = TasksPage(tokens, STATUS_ROWS)
        self.passwords = PasswordPage(tokens, PASSWORD_ROWS)
        self.settings = SettingsPage(tokens)
        self.stack.addWidget(self.tasks)
        self.stack.addWidget(self.passwords)
        self.stack.addWidget(self.settings)
        lay.addWidget(self.stack, 1)
        self.titlebar = TitleBar(tokens, "便捷解压", self)
        self.titlebar.setGeometry(0, 0, 1160, 40)
        self.titlebar.raise_()
        self.nav_tasks.clicked.connect(lambda: self.go("tasks"))
        self.nav_passwords.clicked.connect(lambda: self.go("passwords"))
        self.nav_settings.clicked.connect(lambda: self.go("settings"))
        self.nav_tasks.setChecked(True)
        self.go(page)

    def go(self, page: str):
        mapping = {"tasks": 0, "passwords": 1, "settings": 2}
        self.stack.setCurrentIndex(mapping[page])
        for key, btn in (
            ("tasks", self.nav_tasks),
            ("passwords", self.nav_passwords),
            ("settings", self.nav_settings),
        ):
            btn.setChecked(key == page)

    def paintEvent(self, event):
        p = QPainter(self)
        grad = QLinearGradient(0, 0, 0, self.height())
        grad.setColorAt(0.0, QColor(self.t.window_top))
        grad.setColorAt(0.55, QColor(self.t.window))
        grad.setColorAt(1.0, QColor(self.t.window_bottom))
        p.fillRect(self.rect(), grad)
        p.end()

    def resizeEvent(self, event):
        self.titlebar.setGeometry(0, 0, self.width(), 40)
        self.titlebar.raise_()
        if self._overlay is not None:
            self._overlay.setGeometry(self.rect())
            self._overlay.raise_()
        super().resizeEvent(event)

    def show_overlay(self, card: DialogCard) -> Overlay:
        overlay = Overlay(self, card, self.t)
        self._overlay = overlay
        overlay.raise_()
        return overlay


def render_shell(tokens: Tokens, page: str, name: str, height: int = 760) -> None:
    app = QApplication.instance()
    assert app is not None
    app.setStyleSheet(build_qss(tokens))
    shell = Shell(tokens, page=page, height=height)
    shell.ensurePolished()
    shell.layout().activate()
    QApplication.processEvents()
    pix = shell.grab()
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / name
    pix.save(str(path))
    print(f"{name}: {pix.width()}x{pix.height()}")
    shell.close()


def render_password_dialog(tokens: Tokens) -> None:
    app = QApplication.instance()
    assert app is not None
    app.setStyleSheet(build_qss(tokens))
    shell = Shell(tokens, page="tasks", height=760)
    card = DialogCard(
        tokens,
        "需要密码",
        "“学习资料.rar”已加密。密码库中没有可用密码，请输入密码继续。",
        440,
    )
    field = QLineEdit()
    field.setEchoMode(QLineEdit.EchoMode.Password)
    field.setPlaceholderText("输入密码")
    card.lay.insertWidget(2, field)
    error = QLabel("密码错误，请重试")
    error.setObjectName("ErrorText")
    card.lay.insertWidget(3, error)
    card.add_button("取消")
    card.add_button("继续解压", primary=True)
    card.adjustSize()
    overlay = shell.show_overlay(card)
    overlay.ensurePolished()
    QApplication.processEvents()
    pix = shell.grab()
    path = OUT / "v2_dialog_password.png"
    pix.save(str(path))
    print(f"v2_dialog_password.png: {pix.width()}x{pix.height()}")
    shell.close()


def render_import_dialog(tokens: Tokens) -> None:
    app = QApplication.instance()
    assert app is not None
    app.setStyleSheet(build_qss(tokens))
    shell = Shell(tokens, page="passwords", height=760)
    card = DialogCard(
        tokens,
        "导入密码",
        "从剪贴板识别到 8 条密码，其中 2 条已存在于密码库。",
        500,
    )
    preview = QFrame()
    preview.setObjectName("ImportPreview")
    p_lay = QVBoxLayout(preview)
    p_lay.setContentsMargins(12, 10, 12, 10)
    p_lay.setSpacing(6)
    for pwd, tag in [
        ("x7Kp2mQz", "[剪贴板导入]"),
        ("archive2024", "[剪贴板导入]"),
        ("banzip!88", "[剪贴板导入]"),
        ("f3R9vL0a", "[剪贴板导入]"),
        ("old-backup", "[剪贴板导入]"),
    ]:
        row = QLabel(f"{pwd}    <span style='color:{tokens.text3}'>{tag}</span>")
        row.setObjectName("ImportRow")
        row.setTextFormat(Qt.TextFormat.RichText)
        p_lay.addWidget(row)
    more = QLabel(f"还有 3 条…")
    more.setObjectName("Caption")
    p_lay.addWidget(more)
    card.lay.insertWidget(2, preview)
    card.add_button("取消")
    card.add_button("导入 6 条", primary=True)
    card.adjustSize()
    overlay = shell.show_overlay(card)
    overlay.ensurePolished()
    QApplication.processEvents()
    pix = shell.grab()
    path = OUT / "v2_dialog_import.png"
    pix.save(str(path))
    print(f"v2_dialog_import.png: {pix.width()}x{pix.height()}")
    shell.close()


def render_settings_scrolled(tokens: Tokens, name: str) -> None:
    app = QApplication.instance()
    assert app is not None
    app.setStyleSheet(build_qss(tokens))
    shell = Shell(tokens, page="settings", height=980)
    shell.ensurePolished()
    shell.layout().activate()
    QApplication.processEvents()
    bar = shell.settings.scroll.verticalScrollBar()
    bar.setValue(bar.maximum())
    QApplication.processEvents()
    pix = shell.grab()
    path = OUT / name
    pix.save(str(path))
    print(f"{name}: {pix.width()}x{pix.height()}")
    shell.close()


def render_empty_tasks(tokens: Tokens) -> None:
    app = QApplication.instance()
    assert app is not None
    app.setStyleSheet(build_qss(tokens))
    shell = Shell(tokens, page="tasks", height=760)
    # Replace the tasks page with an empty one for the empty-state mockup.
    empty = TasksPage(tokens, [])
    shell.stack.removeWidget(shell.tasks)
    shell.stack.insertWidget(0, empty)
    shell.stack.setCurrentIndex(0)
    shell.tasks = empty
    QApplication.processEvents()
    pix = shell.grab()
    path = OUT / "v2_tasks_empty.png"
    pix.save(str(path))
    print(f"v2_tasks_empty.png: {pix.width()}x{pix.height()}")
    shell.close()


def main() -> int:
    app = QApplication(sys.argv)
    app.setFont(_font(13))
    # Apply the light stylesheet globally so dialogs inherit it; each render
    # re-applies the matching theme before constructing widgets.
    app.setStyleSheet(build_qss(V2_LIGHT))
    render_shell(V2_LIGHT, "tasks", "v2_tasks_light.png")
    render_shell(V2_LIGHT, "passwords", "v2_passwords_light.png")
    render_shell(V2_LIGHT, "settings", "v2_settings_light.png", height=980)
    render_settings_scrolled(V2_LIGHT, "v2_settings_light_scrolled.png")
    render_empty_tasks(V2_LIGHT)
    render_password_dialog(V2_LIGHT)
    render_import_dialog(V2_LIGHT)
    render_shell(V2_DARK, "tasks", "v2_tasks_dark.png")
    render_shell(V2_DARK, "settings", "v2_settings_dark.png", height=980)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
