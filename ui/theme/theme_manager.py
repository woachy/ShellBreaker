"""主题管理器：全局 QSS 安装、字体设置与当前主题查询。

任何窗口（主窗口 / 两个兼容 wrapper）都必须自己调用 ensure_theme()，
因为测试不会运行 main.py，不能依赖入口设置样式。
"""
from __future__ import annotations

import os

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QWidget

from ui.theme.qss import build_qss
from ui.theme.tokens import DEFAULT_TOKENS, FONT_FAMILIES, Tokens, tokens_for

_CURRENT_THEME: Tokens = DEFAULT_TOKENS
_THEME_PROPERTY = "_shellbreaker_theme_name"


def get_current_theme() -> Tokens:
    """返回当前生效的主题令牌。"""
    return _CURRENT_THEME


def _resolve_theme(theme: Tokens | str | None) -> Tokens:
    if isinstance(theme, Tokens):
        return theme
    if isinstance(theme, str):
        return tokens_for(theme)
    env = os.environ.get("SHELLBREAKER_THEME", "").strip().lower()
    if env:
        return tokens_for(env)
    return _CURRENT_THEME


def _make_app_font() -> QFont:
    font = QFont()
    font.setFamilies(FONT_FAMILIES)
    font.setPixelSize(13)
    # 不要强制 PreferAntialias：那会关掉 ClearType，125%/150% 缩放下粗体发虚、边缘发毛。
    # 交给系统默认策略（有 ClearType 用 ClearType，没有才退回灰度抗锯齿）。
    font.setStyleStrategy(QFont.StyleStrategy.PreferDefault)
    return font


def ensure_theme(
    target: QWidget | QApplication | None = None, theme: Tokens | str | None = None
) -> Tokens:
    """确保全局主题已安装（幂等）。

    Args:
        target: 传入控件时顺带 polish 该控件，使其立即套用样式；
            传入 QApplication 时只安装全局样式（main.py 的用法）。
        theme: 显式主题（Tokens 或 "light"/"dark"）；默认沿用当前主题。
    """
    global _CURRENT_THEME
    active = _resolve_theme(theme)
    app = QApplication.instance()

    if app is not None:
        if app.property(_THEME_PROPERTY) != active.name:
            app.setStyleSheet(build_qss(active))
            app.setFont(_make_app_font())
            app.setProperty(_THEME_PROPERTY, active.name)
        _CURRENT_THEME = active

    if isinstance(target, QWidget):
        style = target.style()
        if style is not None:
            style.unpolish(target)
            style.polish(target)
        target.update()

    return active


def repolish(widget: QWidget) -> None:
    """动态属性变更后刷新 QSS。"""
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()
