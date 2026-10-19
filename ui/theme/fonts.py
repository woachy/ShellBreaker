"""字体工厂：为需要灰度抗锯齿的文本生成 QFont。

背景：QSS 里只要声明了 font-size / font-weight，Qt 就会用「样式表字体」覆盖控件字体，
而样式表字体不携带 QFont.StyleStrategy → 125% 缩放下大号粗体出现 ClearType 彩色描边（毛边）。
因此大号粗体（页面标题、对话框标题）改为在这里造字体，QSS 只负责颜色。
"""
from __future__ import annotations

from PySide6.QtGui import QFont

from ui.theme.tokens import FONT_FAMILIES


def text_font(
    size: int,
    weight: QFont.Weight = QFont.Weight.Normal,
    strategy: QFont.StyleStrategy = QFont.StyleStrategy.PreferAntialias,
    families: list[str] | None = None,
) -> QFont:
    font = QFont()
    font.setFamilies(families or FONT_FAMILIES)
    font.setPixelSize(size)
    font.setWeight(weight)
    font.setStyleStrategy(strategy)
    return font


def display_font(size: int, weight: QFont.Weight = QFont.Weight.DemiBold) -> QFont:
    """大号标题：灰度抗锯齿，避免 ClearType 彩边。"""
    return text_font(size, weight)
