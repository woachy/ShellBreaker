"""Segoe Fluent Icons 字形常量与图标生成工具。

Windows 11 使用 "Segoe Fluent Icons"，Windows 10 回退 "Segoe MDL2 Assets"。
所有图标都实时着色，避免为每种颜色导出图片。
"""
from __future__ import annotations

import sys
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap

# ---- 字形常量（\uE000 私有区，Segoe Fluent Icons 编码） ----
ICON_TASK = "\uE7B8"          # 箱体 / 归档
ICON_KEY = "\uE8D7"
ICON_SETTINGS = "\uE713"
ICON_INFO = "\uE946"
ICON_FOLDER = "\uE8B7"
ICON_FOLDER_OPEN = "\uE838"
ICON_FILE = "\uE8A5"
ICON_ADD = "\uE710"
ICON_DELETE = "\uE74D"
ICON_CLEAN = "\uE74D"
ICON_PASTE = "\uE77F"
ICON_IMPORT = "\uE896"
ICON_MORE = "\uE712"
ICON_CANCEL = "\uE711"
ICON_CHECK = "\uE73E"
ICON_WARNING = "\uE7BA"
ICON_MINIMIZE = "\uE921"
ICON_MAXIMIZE = "\uE922"
ICON_RESTORE = "\uE923"
ICON_CLOSE = "\uE8BB"
ICON_CHEVRON_DOWN = "\uE70D"
ICON_CHEVRON_UP = "\uE70E"
ICON_CHEVRON_RIGHT = "\uE76C"
ICON_REFRESH = "\uE72C"
ICON_EYE = "\uE7B3"
ICON_EYE_HIDE = "\uED1A"
ICON_SEARCH = "\uE721"

_GLYPH_FAMILIES = ("Segoe Fluent Icons", "Segoe MDL2 Assets")


@lru_cache(maxsize=1)
def _glyph_family() -> str:
    available = set(QFontDatabase.families())
    for family in _GLYPH_FAMILIES:
        if family in available:
            return family
    return _GLYPH_FAMILIES[0]


def glyph_font(size: int = 16) -> QFont:
    """返回指定像素大小的字形字体。"""
    font = QFont(_glyph_family())
    font.setPixelSize(size)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return font


def glyph_pixmap(glyph: str, color: str, size: int = 16, dpr: float = 2.0) -> QPixmap:
    """把字形渲染成带透明底的 pixmap（按 dpr 超采样，保证高 DPI 清晰）。"""
    dpr = max(1.0, float(dpr))
    px = max(1, int(round(size * dpr)))
    pixmap = QPixmap(px, px)
    pixmap.setDevicePixelRatio(dpr)
    pixmap.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.setFont(glyph_font(size))
    painter.setPen(QColor(color))
    # 字形在 em 框内偏下，向上微调 1px 使视觉居中
    painter.drawText(
        QRectF(0, -0.5, size, size),
        int(Qt.AlignmentFlag.AlignCenter),
        glyph,
    )
    painter.end()
    return pixmap


def glyph_icon(glyph: str, color: str, size: int = 16) -> QIcon:
    """生成 QIcon（多倍率添加，Qt 自动挑选）。"""
    icon = QIcon()
    for dpr in (1.0, 2.0, 3.0):
        icon.addPixmap(glyph_pixmap(glyph, color, size, dpr))
    return icon


def icon_size(size: int = 16) -> QSize:
    return QSize(size, size)


# ---- 应用图标（标题栏 / 窗口图标复用同一份资产） ----
def _asset_dirs() -> list[Path]:
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            dirs.append(Path(meipass) / "assets")
        dirs.append(Path(sys.executable).parent / "assets")
    dirs.append(Path(__file__).resolve().parents[2] / "assets")
    return dirs


@lru_cache(maxsize=1)
def app_icon() -> QIcon:
    """加载应用图标：优先 .ico（含 16px 简化帧），回退 .png。"""
    icon = QIcon()
    for name in ("app_icon.ico", "app_icon.png"):
        for directory in _asset_dirs():
            candidate = directory / name
            if candidate.exists():
                icon = QIcon(str(candidate))
                if not icon.isNull():
                    return icon
    return icon


def app_icon_pixmap(size: int = 16) -> QPixmap:
    """按目标尺寸取图标位图（.ico 会自动挑最接近的帧）。"""
    icon = app_icon()
    if icon.isNull():
        return QPixmap()
    return icon.pixmap(QSize(size, size))

