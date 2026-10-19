"""状态胶囊委托：把「处理状态」列绘制成 Fluent 胶囊标签。

表格单元格文本保持原样（测试断言依赖 item.text()），着色只发生在绘制层。
状态文本不做归一化：成功 / 失败 / 排队中 / 正在提取 / 验证密码 /
已跳过 / 已跳过(非首卷分卷) / 已跳过(文件过小) 都原样显示。
"""
from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from ui.theme.theme_manager import get_current_theme
from ui.theme.tokens import Tokens

PILL_HEIGHT = 20
PILL_PADDING = 9
LEFT_PADDING = 12


def status_palette(tokens: Tokens, status: str) -> tuple[str, str]:
    """状态文本 → (前景色, 背景色)。"""
    text = (status or "").strip()
    if text.startswith("成功"):
        return tokens.success, tokens.success_bg
    if text.startswith("失败"):
        return tokens.danger, tokens.danger_bg
    if text.startswith("已跳过") or "跳过" in text:
        return tokens.warning, tokens.warning_bg
    if text.startswith("正在提取") or text.startswith("验证密码") or "正在" in text:
        return tokens.accent, tokens.accent_tint
    return tokens.neutral, tokens.neutral_bg


def status_pill_font(base: QFont) -> QFont:
    font = QFont(base)
    font.setPixelSize(11)
    font.setBold(True)
    return font


class StatusPillDelegate(QStyledItemDelegate):
    """第 2 列（处理状态）专用委托。"""

    def paint(self, painter, option, index):  # noqa: D102
        theme = get_current_theme()
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        painter.setRenderHint(painter.RenderHint.TextAntialiasing, True)

        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)

        if selected:
            painter.fillRect(rect, QColor(theme.selection))

        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if text:
            font = status_pill_font(option.font)
            metrics = QFontMetrics(font)
            fg, bg = status_palette(theme, text)
            pill_w = metrics.horizontalAdvance(text) + PILL_PADDING * 2
            pill_w = min(pill_w, rect.width() - LEFT_PADDING * 2)
            x = rect.left() + LEFT_PADDING
            y = rect.top() + (rect.height() - PILL_HEIGHT) / 2.0

            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(bg))
            painter.drawRoundedRect(QRectF(x, y, pill_w, PILL_HEIGHT), 4.0, 4.0)

            painter.setFont(font)
            painter.setPen(QColor(fg))
            painter.drawText(
                QRectF(x, y, pill_w, PILL_HEIGHT),
                int(Qt.AlignmentFlag.AlignCenter),
                text,
            )

        # 行分隔线（QSS 的 item 边框对自绘委托无效，这里补上）
        painter.setPen(QColor(theme.divider))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
        painter.restore()

    def sizeHint(self, option, index) -> QSize:  # noqa: D102
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(size.height(), 44))


class RowTextDelegate(QStyledItemDelegate):
    """通用文本列委托：自绘行背景（选中/悬停）+ 12px 左内边距 + 底部分隔线。

    所有列共用同一套背景绘制，行悬停时整行颜色一致；文本颜色按角色取
    theme.text（主）或 theme.text2（次）。
    """

    TEXT_PADDING = 12

    def __init__(self, parent=None, color_key: str = "text2"):
        super().__init__(parent)
        self._color_key = color_key

    def initStyleOption(self, option: QStyleOptionViewItem, index):  # noqa: N802
        super().initStyleOption(option, index)
        theme = get_current_theme()
        color = theme.text if self._color_key == "text" else theme.text2
        option.palette.setColor(option.palette.ColorRole.Text, QColor(color))
        option.palette.setColor(option.palette.ColorRole.HighlightedText, QColor(theme.text))

    def paint(self, painter, option, index):  # noqa: D102
        theme = get_current_theme()
        painter.save()
        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)

        # 自绘背景，避免原生 Base 色在深色主题下透出浅色块。
        # 只画选中态：悬停态由视图按“单元格”刷新，逐列自绘会出现半行高亮。
        if selected:
            painter.fillRect(rect, QColor(theme.selection))

        text = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        if text:
            painter.setFont(option.font)
            if selected or self._color_key == "text":
                color = theme.text
            else:
                color = theme.text2
            painter.setPen(QColor(color))
            text_rect = rect.adjusted(self.TEXT_PADDING, 0, -8, 0)
            metrics = painter.fontMetrics()
            elided = metrics.elidedText(text, Qt.TextElideMode.ElideRight, text_rect.width())
            painter.drawText(
                text_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                elided,
            )

        painter.setPen(QColor(theme.divider))
        painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
        painter.restore()


class PrimaryTextDelegate(RowTextDelegate):
    """主文本列（文件名 / 密码）：theme.text 颜色。"""

    def __init__(self, parent=None):
        super().__init__(parent, color_key="text")


class SecondaryTextDelegate(RowTextDelegate):
    """次要文本列（引擎 / 备注 / 次数 / 标签）：theme.text2 颜色。"""

    def __init__(self, parent=None):
        super().__init__(parent, color_key="text2")
