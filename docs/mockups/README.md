# Fluent UI 设计稿（规划用）

本目录是 UI 重构的**静态设计稿**，不是生产代码。目的：在动 `ui/` 之前锁定布局、控件、状态和信息架构。

- V1 方案：`docs/UI_REFACTOR_PLAN.md`，输出为 `out/tasks_*.png`、`out/settings_*.png` 等。
- V2 方案：`docs/UI_REFACTOR_PLAN_V2.md`，输出为 `out/v2_*.png`，包含青绿强调色、浅蓝渐变背景、分组式设置页和“便捷解压”新名称/图标。

## 运行

Windows + PySide6（项目已依赖）：

```powershell
$env:QT_SCALE_FACTOR = "2"
python docs/mockups/fluent_prototype.py
python docs/mockups/make_icons.py
```

输出写入 `docs/mockups/out/`。脚本使用假任务/假密码，不读写 `data/config.json` 或 `data/passwords.json`。

## V2 输出

| 文件 | 内容 |
|---|---|
| `out/v2_tasks_light.png` | 浅色任务页（青绿强调色 + 浅蓝渐变） |
| `out/v2_tasks_dark.png` | 深色任务页 |
| `out/v2_tasks_empty.png` | 任务页空状态 |
| `out/v2_passwords_light.png` | 密码库页 |
| `out/v2_settings_light.png` / `out/v2_settings_light_scrolled.png` | 设置页上部/下部（分组卡片 + 自动保存） |
| `out/v2_settings_dark.png` | 深色设置页 |
| `out/v2_dialog_password.png` | 密码输入 ContentDialog（含错误态） |
| `out/v2_dialog_import.png` | 导入预览 ContentDialog |
| `out/icons/icon_concepts.png` | 三个应用图标概念与 16/32/48/256 尺寸测试（C 已选） |
| `out/icons/c_layers_*.png` | 已选图标 C（整体下移 7%）的多尺寸导出 |
| `out/icons/a_unpack_*.png` / `out/icons/b_zipper_*.png` | 备选图标 A/B 的多尺寸导出 |

V1 输出仍保留：`out/tasks_light.png`、`out/settings_light.png`、`out/dialog_password.png` 等，用于对比。

## 已锁定的 V2 设计参数

- 40px 标题栏，260px 导航，页面左右 24 边距，内容最大宽 960。
- 页面标题 28/600；分组卡片白底、1px `#DCE7F1` 边框、圆角 4；行高 64，行间 1px 分隔线。
- 浅色背景渐变 `#FAFDFF → #F4F9FD → #E8F1FA`；强调色 `#0E7490`。
- 设置页采用自动保存：无底部保存/取消，右上角显示“✓ 所有更改已保存”；兼容层仍保留 `SettingsDialog._on_save()`。
- 表格行高 44、表头 36；4 列文案与现有测试一致。
- 图标使用 Segoe Fluent Icons；本机已核对 20 个字形。生产实现需要 Win10 MDL2 回退。
- 中文显示名“便捷解压”；技术标识仍为 `ShellBreaker`。

## 已知限制（设计稿层面）

- 假数据；不包含真实任务线程、拖放、密码库读写。
- 不包含动画、焦点环、Mica、系统主题监听；这些在实现阶段做。
- 字体使用系统 `Segoe UI Variable Text` + 中文回退；不同机器字形宽度可能略有差异。
- `fluent_prototype.py` 里有些为静态渲染写的简化（例如固定窗口尺寸、直接 `setStyleSheet`）；**不要直接复制到 `ui/`**，生产实现按规划文档的第 9 节拆分。

## 与规划文档的关系

- V1 规划：`docs/UI_REFACTOR_PLAN.md`
- V2 规划：`docs/UI_REFACTOR_PLAN_V2.md`
- 控制映射：V1 文档第 7 节（M01–M23 / S01–S13 / P01–P10），V2 只改设置页的呈现/保存方式，不删按钮。
- 设计与 API 约束：V1 文档第 5、8 节；V2 文档第 3、8、9 节。
