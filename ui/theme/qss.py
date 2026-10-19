"""QSS 生成器：把 Tokens 注入模板，产出单一全局样式表。

约束：
- 只使用 @token 占位符，不在模板里写死颜色；
- 背景渐变由 Shell.paintEvent 绘制，所有容器必须透明；
- 下拉箭头 / 步进按钮由自绘控件负责，这里负责隐藏原生装饰。
"""
from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from ui.theme.tokens import Tokens


_QSS_TEMPLATE = """
/* ============ 基础 ============ */
/* 注意：这里不要声明 font-size / font-family！
   一旦在全局选择器上写字体属性，Qt 会给每个控件套一份“样式表字体”，
   丢失 QApplication 字体上的 PreferAntialias 策略 → 125% 缩放下粗体出现彩边（毛边）。
   字号统一由 theme_manager._make_app_font() 的 pixelSize=13 提供。 */
QWidget {
    color: @text;
}

QToolTip {
    background: @card;
    color: @text;
    border: 1px solid @card_border;
    border-radius: 4px;
    padding: 4px 8px;
    font-size: 12px;
}

/* ============ 透明容器（渐变由 Shell 绘制） ============ */
QWidget#Root,
QWidget#Shell,
QWidget#Content,
QWidget#Page,
QWidget#ScrollBody,
QStackedWidget,
QScrollArea,
QScrollArea > QWidget > QWidget,
QWidget#TasksPage,
QWidget#PasswordsPage,
QWidget#SettingsPage {
    background: transparent;
}

QLabel {
    background: transparent;
}

/* ============ 标题栏 ============ */
QWidget#TitleBar {
    background: transparent;
}

QLabel#AppTitle {
    font-size: 12px;
    color: @text2;
}

QLabel#AppIcon {
    background: transparent;
}

QPushButton#CaptionButton {
    background: transparent;
    border: none;
    border-radius: 0px;
}

QPushButton#CaptionButton:hover {
    background: @caption_hover;
}

QPushButton#CaptionButton:pressed {
    background: @control_pressed;
}

QPushButton#CaptionClose:hover {
    background: #C42B1C;
}

QPushButton#CaptionClose:pressed {
    background: #A92716;
}

/* ============ 导航 ============ */
QFrame#Nav {
    background: @nav;
    border: none;
    border-right: 1px solid @divider;
}

QLabel#NavSection {
    font-size: 11px;
    color: @text3;
    font-weight: 600;
}

QPushButton#NavItem {
    background: transparent;
    border: none;
    border-left: 3px solid transparent;
    border-radius: 0px;
    color: @text2;
    font-size: 13px;
    text-align: left;
    padding-left: 13px;
    padding-right: 8px;
    outline: none;
}

QPushButton#NavItem:hover {
    background: @nav_hover;
    color: @text;
}

QPushButton#NavItem:checked {
    background: @nav_checked;
    border-left: 3px solid @accent;
    color: @text;
    font-weight: 600;
}

QPushButton#NavItem:checked:hover {
    background: @nav_checked;
}

QPushButton#NavItem[collapsed="true"] {
    padding-left: 0px;
    padding-right: 0px;
    text-align: center;
}

QPushButton#NavItem:focus {
    outline: none;
    border-left: 3px solid transparent;
}

/* ============ 页面文字 ============ */
/* PageTitle 的字体在代码里设置，见 ui/theme/fonts.py 的说明 */

QLabel#PageSubtitle {
    font-size: 13px;
    color: @text2;
}

/* CardTitle / SectionTitle 的字体同样在代码里设置（灰度抗锯齿） */

QLabel#SectionDesc {
    font-size: 12px;
    color: @text2;
}

QLabel#RowTitle {
    font-size: 13px;
}

QLabel#RowDesc {
    font-size: 12px;
    color: @text2;
}

QLabel#RowDesc[muted="true"] {
    color: @text3;
}

QLabel#ErrorText {
    font-size: 12px;
    color: @danger;
}

QLabel#SavedStatus {
    font-size: 12px;
    color: @text3;
}

QLabel#SavedStatus[state="invalid"] {
    color: @danger;
}

QLabel#SavedStatus[state="saving"] {
    color: @text2;
}

QLabel#StatusText {
    font-size: 12px;
    color: @text2;
}

QLabel#CountBadge {
    background: @neutral_bg;
    color: @text2;
    border-radius: 9px;
    padding: 0px 7px;
    font-size: 11px;
    font-weight: 600;
}

QLabel#EmptyTitle {
    font-size: 14px;
    font-weight: 600;
    color: @text2;
}

QLabel#EmptyDesc {
    font-size: 12px;
    color: @text3;
}

QLabel#MonoText {
    font-family: "Cascadia Mono", "Consolas", monospace;
    font-size: 13px;
}

/* ============ 卡片 ============ */
QFrame#Card,
QFrame#GroupCard {
    background: @card;
    border: 1px solid @card_border;
    border-radius: 4px;
}

QFrame#RowDivider {
    background: @divider;
    border: none;
}

QFrame#StatusBar {
    background: transparent;
    border: none;
    border-top: 1px solid @divider;
}

/* ============ 拖放卡 ============ */
QFrame#DropCard {
    background: @card;
    border: 1px solid @card_border;
    border-radius: 4px;
}

QFrame#DropCard[drag="true"] {
    background: @accent_tint;
    border: 1px dashed @accent;
}

QLabel#DropIconBox {
    background: @accent_tint;
    border: none;
    border-radius: 8px;
}

/* 大号粗体（DropTitle / PageTitle / DialogTitle）的字号与字重由
   ui/theme/fonts.py 在代码里设置：QSS 一旦声明字体属性，样式表字体会丢掉
   灰度抗锯齿策略，125% 缩放下大号粗体出现 ClearType 彩边（毛边）。 */

QLabel#DropSubtitle {
    font-size: 12px;
    color: @text2;
}

/* ============ 按钮 ============ */
QPushButton {
    background: @control;
    border: 1px solid @control_border;
    border-radius: 4px;
    color: @text;
    font-size: 13px;
    min-height: 30px;
    padding: 0px 14px;
}

QPushButton:hover {
    background: @control_hover;
}

QPushButton:pressed {
    background: @control_pressed;
}

/* 键盘焦点：只改描边颜色，避免 1px 位移抖动 */
QPushButton:focus {
    border-color: @accent;
}

QPushButton#Primary:focus {
    border-color: @accent_text;
}

QPushButton#Subtle:focus,
QPushButton#DangerSubtle:focus,
QPushButton#IconButton:focus {
    border-color: @accent;
}

QPushButton:disabled {
    background: @row_hover;
    color: @text3;
    border-color: @divider;
}

QPushButton#Primary {
    background: @accent;
    border: 1px solid @accent;
    color: @accent_text;
    font-weight: 600;
}

QPushButton#Primary:hover {
    background: @accent_hover;
    border-color: @accent_hover;
}

QPushButton#Primary:pressed {
    background: @accent_pressed;
    border-color: @accent_pressed;
}

QPushButton#Primary:disabled {
    background: @divider;
    border-color: @divider;
    color: @text3;
}

QPushButton#Secondary {
    background: @control;
    border: 1px solid @control_border;
    color: @text;
}

QPushButton#Secondary:hover {
    background: @control_hover;
}

QPushButton#Subtle {
    background: transparent;
    border: 1px solid transparent;
    color: @text2;
    padding: 0px 10px;
}

QPushButton#Subtle:hover {
    background: @control_hover;
    color: @text;
}

QPushButton#Subtle:pressed {
    background: @control_pressed;
}

QPushButton#Subtle:disabled {
    background: transparent;
    color: @text3;
}

QPushButton#DangerSubtle {
    background: transparent;
    border: 1px solid transparent;
    color: @danger;
    padding: 0px 10px;
}

QPushButton#DangerSubtle:hover {
    background: @danger_bg;
}

QPushButton#IconButton {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 0px;
}

QPushButton#IconButton:hover {
    background: @control_hover;
}

QPushButton#SpinStep {
    background: transparent;
    border: none;
    border-radius: 3px;
    min-height: 0px;
    padding: 0px;
}

QPushButton#SpinStep:hover {
    background: @control_hover;
}

QPushButton#SpinStep:pressed {
    background: @control_pressed;
}

/* ============ 输入控件 ============ */
QLineEdit,
QComboBox,
QSpinBox {
    background: @control;
    border: 1px solid @control_border;
    border-radius: 4px;
    color: @text;
    font-size: 13px;
    min-height: 30px;
    padding: 0px 8px;
    selection-background-color: @accent;
    selection-color: @accent_text;
}

QLineEdit:hover,
QComboBox:hover,
QSpinBox:hover {
    border-color: @accent;
}

QLineEdit:focus,
QComboBox:focus,
QSpinBox:focus {
    border: 2px solid @accent;
    padding: 0px 7px;
}

QLineEdit:disabled,
QComboBox:disabled,
QSpinBox:disabled {
    background: @row_hover;
    border-color: @divider;
    color: @text3;
}

QLineEdit[error="true"],
QComboBox[error="true"] {
    border-color: @danger;
}

QComboBox {
    padding-right: 28px;
}

QComboBox::drop-down {
    subcontrol-origin: padding;
    subcontrol-position: top right;
    width: 28px;
    border: none;
    background: transparent;
}

QComboBox::down-arrow {
    image: none;
    width: 0px;
    height: 0px;
}

QComboBox QAbstractItemView {
    background: @card;
    border: 1px solid @card_border;
    border-radius: 4px;
    color: @text;
    outline: none;
    padding: 4px;
    selection-background-color: @selection;
    selection-color: @text;
}

QComboBox QAbstractItemView::item {
    min-height: 28px;
    padding-left: 8px;
    border-radius: 3px;
}

QSpinBox::up-button,
QSpinBox::down-button {
    width: 0px;
    height: 0px;
    border: none;
}

QSpinBox::up-arrow,
QSpinBox::down-arrow {
    width: 0px;
    height: 0px;
    image: none;
}

/* ============ 表格 ============ */
QTableWidget {
    background: transparent;
    alternate-background-color: transparent;
    border: none;
    gridline-color: transparent;
    outline: none;
}

QTableWidget::item {
    border-bottom: 1px solid @divider;
    color: @text;
    padding-left: 12px;
    padding-right: 8px;
}

QTableWidget::item:hover {
    background: @row_hover;
}

QTableWidget::item:selected {
    background: @selection;
    color: @text;
}

QHeaderView {
    background: @header_bg;
    border: none;
}

QHeaderView::section {
    background: @header_bg;
    border: none;
    border-bottom: 1px solid @divider;
    color: @text2;
    font-size: 11px;
    font-weight: 600;
    padding-left: 12px;
    padding-right: 8px;
}

QHeaderView::section:hover {
    background: @header_bg;
}

QTableCornerButton::section {
    background: @header_bg;
    border: none;
}

/* ============ 滚动条 ============ */
QScrollBar:vertical {
    background: transparent;
    width: 12px;
    margin: 2px 2px 2px 0px;
}

QScrollBar::handle:vertical {
    background: @control_border;
    border-radius: 4px;
    min-height: 32px;
}

QScrollBar::handle:vertical:hover {
    background: @text3;
}

QScrollBar::add-line:vertical,
QScrollBar::sub-line:vertical {
    height: 0px;
    background: transparent;
}

QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical {
    background: transparent;
}

QScrollBar:horizontal {
    background: transparent;
    height: 12px;
    margin: 0px 2px 2px 2px;
}

QScrollBar::handle:horizontal {
    background: @control_border;
    border-radius: 4px;
    min-width: 32px;
}

QScrollBar::handle:horizontal:hover {
    background: @text3;
}

QScrollBar::add-line:horizontal,
QScrollBar::sub-line:horizontal {
    width: 0px;
    background: transparent;
}

QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {
    background: transparent;
}

/* ============ 菜单 ============ */
QMenu {
    background: @card;
    border: 1px solid @card_border;
    border-radius: 4px;
    padding: 4px;
}

QMenu::item {
    background: transparent;
    border-radius: 4px;
    color: @text;
    padding: 7px 28px 7px 10px;
}

QMenu::item:selected {
    background: @selection;
}

QMenu::item:disabled {
    color: @text3;
}

QMenu::separator {
    background: @divider;
    height: 1px;
    margin: 4px 8px;
}

/* ============ 对话框 ============ */
QDialog {
    background: transparent;
}

QFrame#DialogCard {
    background: @card;
    border: 1px solid @card_border;
    border-radius: 8px;
}

/* DialogTitle 的字体在代码里设置，见 ui/theme/fonts.py 的说明 */

QLabel#DialogMessage {
    font-size: 13px;
    color: @text2;
}

QFrame#InfoBar {
    background: @accent_tint;
    border: 1px solid @card_border;
    border-radius: 4px;
}

QFrame#InfoBar[severity="success"] {
    background: @success_bg;
    border-color: @success_bg;
}

QFrame#InfoBar[severity="error"] {
    background: @danger_bg;
    border-color: @danger_bg;
}

QFrame#InfoBar[severity="warning"] {
    background: @warning_bg;
    border-color: @warning_bg;
}
"""


def build_qss(tokens: "Tokens") -> str:
    """把 token 注入模板；长键优先替换，避免前缀互相污染。"""
    values = asdict(tokens)
    qss = _QSS_TEMPLATE
    for key in sorted(values, key=len, reverse=True):
        qss = qss.replace(f"@{key}", str(values[key]))
    return qss
