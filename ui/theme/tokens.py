"""便捷解压 V2 设计令牌 (Design Tokens)。

单一事实来源：颜色 / 尺寸 / 字体。
QSS 与自绘控件都从这里取值，禁止在页面里硬编码颜色。

浅色为基线；深色为等价替换（同一套 token 名称）。
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Tokens:
    """一套完整主题的颜色令牌。"""

    name: str

    # ---- 窗口与表面 ----
    window_top: str          # 背景渐变起点
    window: str              # 背景渐变中点
    window_bottom: str       # 背景渐变终点
    nav: str                 # 左侧导航底色
    nav_hover: str           # 导航项 hover
    nav_checked: str         # 导航项选中底色
    card: str                # 卡片/表面
    card_border: str         # 卡片描边
    header_bg: str           # 表头 / 次级表面
    divider: str             # 分隔线
    row_hover: str           # 行 hover / 禁用表面
    selection: str           # 选中底色

    # ---- 文本 ----
    text: str                # 主文本
    text2: str               # 次要文本
    text3: str               # 三级文本 / 占位

    # ---- 强调色 ----
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_text: str         # 强调色之上的文字
    accent_tint: str         # 淡强调底

    # ---- 输入控件 ----
    control: str
    control_border: str
    control_hover: str
    control_pressed: str

    # ---- 语义状态 ----
    success: str
    success_bg: str
    danger: str
    danger_bg: str
    warning: str
    warning_bg: str
    neutral: str
    neutral_bg: str

    # ---- 杂项 ----
    shadow: str
    caption_hover: str       # 标题栏按钮 hover


LIGHT = Tokens(
    name="light",
    window_top="#FAFDFF",
    window="#F4F9FD",
    window_bottom="#E8F1FA",
    nav="#F3F9FD",
    nav_hover="#EAF3FB",
    nav_checked="#E4F2F8",
    card="#FFFFFF",
    card_border="#DCE7F1",
    header_bg="#F6FAFD",
    divider="#E7EFF6",
    row_hover="#F7FBFE",
    selection="#EAF5F9",
    text="#16212B",
    text2="#52616F",
    text3="#7E8D9A",
    accent="#0E7490",
    accent_hover="#1483A0",
    accent_pressed="#0A5A70",
    accent_text="#FFFFFF",
    accent_tint="#E6F4F8",
    control="#FFFFFF",
    control_border="#C9D8E5",
    control_hover="#F5FAFD",
    control_pressed="#EEF4FA",
    success="#0F7B0F",
    success_bg="#DFF6DD",
    danger="#C42B1C",
    danger_bg="#FDE7E9",
    warning="#9D5D00",
    warning_bg="#FFF4CE",
    neutral="#5D5D5D",
    neutral_bg="#EEF2F6",
    shadow="#B9CBD9",
    caption_hover="#E7F0F7",
)


DARK = Tokens(
    name="dark",
    window_top="#242C32",
    window="#1E2429",
    window_bottom="#191F24",
    nav="#1B2228",
    nav_hover="#242C33",
    nav_checked="#1B3A42",
    card="#262E34",
    card_border="#3A464F",
    header_bg="#20272D",
    divider="#344048",
    row_hover="#2B353C",
    selection="#1B3A42",
    text="#F4F8FA",
    text2="#C3CDD4",
    text3="#8C9AA4",
    accent="#5BC8D8",
    accent_hover="#74D4E1",
    accent_pressed="#45B3C3",
    accent_text="#071A1F",
    accent_tint="#17343B",
    control="#2B343B",
    control_border="#46545E",
    control_hover="#323D45",
    control_pressed="#3A464F",
    success="#6CCB5F",
    success_bg="#1E3A20",
    danger="#FF99A4",
    danger_bg="#3A1F22",
    warning="#FCE100",
    warning_bg="#3A3413",
    neutral="#C5C5C5",
    neutral_bg="#2E363C",
    shadow="#0B0F12",
    caption_hover="#323D45",
)


# V2 与基础令牌共用同一套名称；保留别名以便测试/外部引用。
V2_LIGHT = replace(LIGHT, name="v2-light")
V2_DARK = replace(DARK, name="v2-dark")

DEFAULT_TOKENS = V2_LIGHT


# ---- 尺寸常量（非颜色，供布局代码引用） ----
TITLE_BAR_HEIGHT = 40
NAV_WIDTH = 260
NAV_COLLAPSED_WIDTH = 48
NAV_COLLAPSE_THRESHOLD = 1008
STATUS_BAR_HEIGHT = 36
PAGE_MARGIN = 24
CONTENT_MAX_WIDTH = 960
CONTROL_HEIGHT = 32
SETTING_ROW_HEIGHT = 64
TABLE_ROW_HEIGHT = 44
TABLE_HEADER_HEIGHT = 36
CARD_RADIUS = 4
DIALOG_WIDTH = 440

FONT_FAMILIES = [
    "Segoe UI Variable Text",
    "Microsoft YaHei UI",
    "Segoe UI",
    "sans-serif",
]
MONO_FAMILIES = ["Cascadia Mono", "Consolas", "Courier New", "monospace"]


def tokens_for(name: str) -> Tokens:
    """按名称取主题；未知名称回退浅色。"""
    return V2_DARK if str(name).lower() in ("dark", "v2-dark") else V2_LIGHT
