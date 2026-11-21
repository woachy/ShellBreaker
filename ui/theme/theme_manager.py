"""主题管理器：全局 QSS 安装、字体设置、当前主题查询与运行时动态换肤。

任何窗口（主窗口 / 两个兼容 wrapper）都必须自己调用 ensure_theme()，
因为测试不会运行 main.py，不能依赖入口设置样式。
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QApplication, QWidget

from ui.theme.qss import build_qss
from ui.theme.tokens import DEFAULT_TOKENS, FONT_FAMILIES, Tokens, tokens_for

_CURRENT_THEME: Tokens = DEFAULT_TOKENS
_THEME_PROPERTY = "_shellbreaker_theme_name"
_RUNTIME_OVERRIDE: bool = False


class ThemeNotifier(QObject):
    """全局主题切换信号广播器（单例）。"""

    theme_changed = Signal(str)


_THEME_NOTIFIER: Optional[ThemeNotifier] = None


def get_theme_notifier() -> ThemeNotifier:
    """获取全局 ThemeNotifier 单例。"""
    global _THEME_NOTIFIER
    if _THEME_NOTIFIER is None:
        _THEME_NOTIFIER = ThemeNotifier()
    return _THEME_NOTIFIER


def _normalize_mode(mode: str | Tokens | None) -> str:
    """将主题名称或令牌规范化为 'dark' 或 'light'。"""
    if isinstance(mode, Tokens):
        return "dark" if "dark" in (mode.name or "").lower() else "light"
    text = str(mode or "").strip().lower()
    return "dark" if text in ("dark", "v2-dark") else "light"


def _read_config_theme() -> str:
    """安全读取 ConfigManager 中持久化的 theme 配置，失败回退 'light'。"""
    try:
        from data.config_manager import ConfigManager

        val = ConfigManager().get("theme", "light")
        return _normalize_mode(val)
    except Exception:
        return "light"


def get_current_theme() -> Tokens:
    """返回当前生效的主题令牌。"""
    return _CURRENT_THEME


def _resolve_theme(
    theme: Tokens | str | None,
    *,
    read_config: bool = False,
) -> Tokens:
    if isinstance(theme, Tokens):
        return theme
    if isinstance(theme, str) and theme.strip():
        return tokens_for(theme.strip())

    env = os.environ.get("SHELLBREAKER_THEME", "").strip().lower()
    if env in ("dark", "light", "v2-dark", "v2-light"):
        if read_config or not _RUNTIME_OVERRIDE:
            return tokens_for(env)

    if read_config:
        return tokens_for(_read_config_theme())

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

    启动确定主题优先级：
    1. 显式传入的 theme 参数；
    2. 环境变量 SHELLBREAKER_THEME（'dark' / 'light'）；
    3. ConfigManager().get("theme", "light")；
    4. 兜底 'light'。

    Args:
        target: 传入控件时顺带 polish 该控件，使其立即套用样式；
            传入 QApplication 时按启动优先级安装全局样式（main.py 的用法）。
        theme: 显式主题（Tokens 或 "light"/"dark"）；默认沿用当前主题或启动优先级。
    """
    global _CURRENT_THEME, _RUNTIME_OVERRIDE
    app = target if isinstance(target, QApplication) else QApplication.instance()
    is_app_init = isinstance(target, QApplication) or app is None or app.property(_THEME_PROPERTY) is None
    if isinstance(target, QApplication) and theme is None:
        _RUNTIME_OVERRIDE = False

    active = _resolve_theme(theme, read_config=is_app_init)

    if app is not None:
        if app.property(_THEME_PROPERTY) != active.name:
            app.setStyleSheet(build_qss(active))
            app.setFont(_make_app_font())
            app.setProperty(_THEME_PROPERTY, active.name)
        _CURRENT_THEME = active
    else:
        _CURRENT_THEME = active

    if isinstance(target, QWidget):
        style = target.style()
        if style is not None:
            style.unpolish(target)
            style.polish(target)
        target.update()

    return active


def set_theme(mode: str, app: Optional[QApplication] = None) -> str:
    """运行时动态切换全局浅色 / 深色主题，并广播 theme_changed 信号。

    Args:
        mode: 'dark' 或 'light'（其他未知值规范化为 'light'）。
        app: 可选目标 QApplication，默认使用 QApplication.instance()。

    Returns:
        最终生效的主题字符串（'dark' 或 'light'）。
    """
    global _CURRENT_THEME, _RUNTIME_OVERRIDE
    normalized = _normalize_mode(mode)
    tokens = tokens_for(normalized)
    _CURRENT_THEME = tokens
    _RUNTIME_OVERRIDE = True

    target_app = app if app is not None else QApplication.instance()
    if target_app is not None:
        target_app.setStyleSheet(build_qss(tokens))
        target_app.setFont(_make_app_font())
        target_app.setProperty(_THEME_PROPERTY, tokens.name)

    get_theme_notifier().theme_changed.emit(normalized)
    return normalized


def repolish(widget: QWidget) -> None:
    """动态属性变更后刷新 QSS。"""
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()
