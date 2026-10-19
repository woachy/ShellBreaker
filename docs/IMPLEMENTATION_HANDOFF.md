# 便捷解压 V2 实施对接文档

> **状态更新（2026-10-19 深夜）**：用户先用 Gemini 3.8 Flash 跑了一版 V2。当前工作区已有该实现（`ui/theme/`、`ui/widgets/`、`ui/pages/` 等新增，`ui/main_window.py` 等修改），`python -m pytest -q` 为 **42 passed**，但视觉审计不通过：表头被空状态层盖住、设置页宽度塌缩、原生 QSpinBox 上下键不可靠、QComboBox 无箭头、仍是原生标题栏、字体/控件质感不统一。
> **本轮建议不是逐项修补，而是重写 UI 表现层**：保留 `core/`、`data/`、`tests/`、`MainWindow` 业务方法、两个兼容 wrapper 和信号；重写 `ui/theme/`、`ui/widgets/`、`ui/pages/` 与 `MainWindow` 壳。用户尚未明确说“开始重写”。
> 本文件最初写于“干净仓库”状态，下面的 P0–P4 仍适用，但执行前必须先读 §1.1 的当前实现审计。

---

## 1. 先读什么

按顺序：

1. [`docs/UI_REFACTOR_PLAN_V2.md`](UI_REFACTOR_PLAN_V2.md)：V2 设计、token、设置页、图标、阶段。
2. [`docs/UI_REFACTOR_PLAN.md`](UI_REFACTOR_PLAN.md) 第 7、8 节：V1 的完整控制映射、API/信号/配置清单、兼容层约束。
3. [`docs/mockups/README.md`](mockups/README.md)：设计稿说明、运行方式、已知限制。
4. 本文件：实施顺序、硬约束、验收命令。

设计稿不是生产代码。**不要把 `docs/mockups/fluent_prototype.py` 直接复制进 `ui/`**；它为了静态渲染做了简化（固定尺寸、直接 setStyleSheet、假数据）。

### 1.1 当前 Gemini 实现审计（先读，避免在坏结构上补丁）

已确认的问题：

| # | 位置 | 问题 |
|---|---|---|
| 1 | `ui/pages/tasks_page.py:95-121`、`:145-156` | `empty_widget` 是 `task_card` 的子控件，`setGeometry(self.table.geometry())` 后 `raise_()`，把表头一起盖住；截图里“文件名/处理状态/使用引擎”消失、“任务”标题不见，只剩“详细备注”。这不是图标遮挡，是空状态层覆盖。 |
| 2 | `ui/pages/settings_page.py:59-64`、`:238` | `container.setMaximumWidth(960)` + `scroll_lay.addWidget(container, alignment=AlignHCenter)`，container 没有 Expanding size policy，宽度塌到内容宽（约 660），造成“设置页宽度莫名变动”和大片右侧空白。 |
| 3 | `ui/widgets/number_box.py:15-34`、`ui/theme/qss.py:339-351` | `NumberBox` 直接继承 `QSpinBox` 并用原生 `UpDownArrows`；QSS 只样式化 `QSpinBox` 基类，没有 `::up-button/::down-button/::up-arrow/::down-arrow`。原生上下键又大又丑，点击目标不可靠，用户反馈“只能滚轮”。 |
| 4 | `ui/theme/qss.py:304-327` | 只设置了 `QComboBox::drop-down` 宽度，没有 `::down-arrow` 图像/箭头；组合框看起来像纯文本框，截图“自定义目录”无下拉箭头。 |
| 5 | `ui/main_window.py:89-181` | 仍是原生 Windows 标题栏，没有 V2 的 40px Fluent 标题栏；截图顶部就是系统边框。 |
| 6 | `ui/theme/qss.py:12-16` + 多处 inline style | 全局 `*` 字体、页面/分组标题 600、中文 fallback 叠加，字体粗细不统一；`tasks_page.py:63-65,108-112`、`settings_page.py:286-296`、`content_dialog.py:55-61` 还有硬编码颜色。 |
| 7 | `ui/widgets/drop_card.py:22-73` | 固定 128px、56px 图标；“选择目录”次级按钮边框太弱、图标/文字/按钮拥挤。 |
| 8 | `ui/widgets/toggle_switch.py:15-91` | 自绘 40×20 QCheckBox 功能可用，但文字是手绘，缺少暗色/禁用/高 DPI 细节。 |
| 9 | `ui/widgets/content_dialog.py:26-97` | 硬编码浅色 `#16212B` / `#52616F`，无阴影，深色主题会脏。 |

可保留的部分：

- `core/`、`data/`、`tests/` 未变，42 tests pass。
- `MainWindow` 的拖放过滤、任务行、右键、密码供给、`settingsChanged` 业务逻辑基本完整。
- `SettingsPage` 的自动保存/校验/`flush()` 逻辑可复用。
- `SettingsDialog` / `PasswordDialog` wrapper 仍保留测试属性。
- **测试通过只证明兼容层，不证明 UI 质量。**

执行策略：

1. 新建分支 `ui/v2-reset`，保留当前实现可回退。
2. 不逐项修补上表；按 V2 设计重写 `ui/theme/`、`ui/widgets/`、`ui/pages/` 和 `MainWindow` 壳。
3. 从现有 `MainWindow` 复制业务方法/信号连接，从 `SettingsPage` 复制自动保存逻辑，从 `TasksPage` 复制任务行逻辑；不要复制布局和样式。
4. 先做“标题栏 + 任务页”可运行视觉 spike，截图对 V2 mockup；确认后再铺设置页/密码页。

---

## 2. 已确认的产品决策

| 项 | 决策 |
|---|---|
| 中文显示名 | **便捷解压**；只改显示名，技术名 `ShellBreaker` / `shellbreaker` / EXE / `data/` 不动 |
| 应用图标 | **C 层叠剥离**；图形整体下移 7% 做视觉居中；16/20/24 单独简化 |
| 设置保存 | **自动保存**；无底部保存/取消；右上角“✓ 所有更改已保存”；兼容层保留 `SettingsDialog._on_save()` |
| 强调色 | `#0E7490`（hover `#1483A0`，pressed `#0A5A70`，tint `#E6F4F8`，selection `#EAF5F9`） |
| 背景 | 浅色纵向渐变 `#FAFDFF → #F4F9FD → #E8F1FA`；深色中性渐变 |
| 导航 | 260px 展开；<1008px 收起 48px 图标栏；默认窗口 1160×760，最小 900×600 |
| 设置页 | 24px 边距、内容最大宽 960、分组卡、64px 行、1px 分隔 |
| 页面结构 | 解压任务 / 密码库 / 设置三页；密码与设置保留兼容 wrapper |
| 不做的 | 任务搜索、密码搜索、检查更新、开机启动、About 页、`disguise_min_size_mb` UI、引擎路径手输 |

图标资产：

- 概念图：`docs/mockups/out/icons/icon_concepts.png`
- 已选 C：`docs/mockups/out/icons/c_layers.png` 及 `_16/_32/_48/_256.png`
- 设计稿标题栏用的选中图标：`docs/mockups/assets/app_icon_v2.png`
- 生产替换：`assets/app_icon.png`（256×256）与 `assets/app_icon.ico`（16/24/32/48/64/128/256）；路径不变，`ShellBreaker.spec` 的 `datas` 不用改。

---

## 3. 不可破坏的兼容层（最高优先级）

现有 `tests/test_feature_updates.py:585-746` 直接访问 UI 内部结构。**先读这段测试，再动手。** 目标：不改测试断言，全部通过。

`MainWindow` 必须保留：

```python
_table                 # QTableWidget，4 列
_task_rows             # dict[str, int]
_task_data             # dict[str, dict]
_cb_delete_source      # 可 setChecked 的 QCheckBox 子类
_on_task_status_changed(task_id, filename, status, engine, remark)
dropEvent(event)
_on_drop_paths_ready(paths)
```

`SettingsDialog` 必须保留：

```python
_cb_delete_source      # 属性代理到 SettingsPage 的 ToggleSwitch
_on_save()             # 强制 flush 当前控件值并写盘；返回 bool
```

`PasswordDialog` 必须保留：

```python
_table                 # 3 列
# 表头精确："密码（明文）" / "使用次数" / "标签"
_btn_add
_btn_import_clip
_btn_import_file
_btn_delete
_btn_clear
_refresh_table()
```

其他硬约束：

- 控件必须在**构造后立即可访问**，不能懒加载页面。
- `ToggleSwitch` 必须继承 `QCheckBox`，`setChecked()` / `toggled` 正常发信号。
- `SettingsPage.save()` / `flush()` **不管 dirty 标志**，每次调用都写当前控件值；测试会 `setChecked(True)` 后直接 `_on_save()`。
- 表格 item 文本保留原始状态字符串，不归一化：
  - `"已跳过"`、`"已跳过(非首卷分卷)"`、`"已跳过(文件过小)"` 都按“跳过”统计，但显示文本不能改写。
- 拖放只处理一次：Shell 或 DropCard 只保留一个 drop handler，避免一次拖入生成两条任务。
- 主题必须在 `MainWindow.__init__` 和两个 wrapper 里 `ensure_theme()`；测试不跑 `main.py`，不能只在 `main.py` 设样式。

---

## 4. 接口、信号、配置（不得漏/不得虚构）

### 4.1 TaskQueue

`core/task_queue.py:52-55`：

```python
task_status_changed = Signal(str, str, str, str, str)  # task_id, filename, status, engine, remark
task_progress = Signal(str, str)                       # task_id, progress_text
password_needed = Signal(str, str)                     # task_id, archive_name
password_wrong = Signal(str)                           # task_id
```

UI 使用的方法：

```python
submit_existing(task_id, src)
scan_directory(dir)
supply_password(task_id, password_or_none)
reload_config()
shutdown(wait=False)
_is_file_too_small(path)
```

### 4.2 状态词

pipeline 实际发射：`"正在提取"`、`"成功"`、`"失败"`、`"验证密码"`。
UI 另加：`"排队中"`、`"已跳过"`、`"已跳过(非首卷分卷)"`、`"已跳过(文件过小)"`。
统计只依赖前缀；颜色映射沿用现有 `STATUS_COLORS` 语义。

### 4.3 ConfigManager

`data/config_manager.py:19-30` 的键：

```text
default_engine, output_mode, custom_output_path,
max_workers, max_scan_depth, min_file_size_mb, disguise_min_size_mb,
bandizip_path, winrar_path, delete_source_after_extract
```

`reset_defaults()` 在 `:91-94`，会直接保存。
自动保存流程：先校验 → `set(key, value)` → `save()`；失败恢复旧值。
`max_workers` / 引擎变化才调用 `_task_queue.reload_config()`；不要每个设置项都重启线程池。

### 4.4 PasswordManager

`get_all()`、`add()`、`record_hit()`、`batch_add()`、`delete_by_password()`、`delete()`、`clear_all()`、`count`；模块函数 `parse_passwords_text()`、`read_text_safe()`。不要改签名。

### 4.5 引擎

`get_engine_display_name()` 返回 `"Bandizip (bz.exe)"` / `"WinRAR"`；`discover_engine()` 尊重 `bandizip_path` / `winrar_path` / `default_engine`。

---

## 5. 实施顺序（建议按阶段提交）

### P0 设计系统与身份（0.5–1 天）

- 新增 `ui/theme/tokens.py`、`qss.py`、`theme_manager.py`、`icons.py`。
- 实现 `V2_LIGHT` / `V2_DARK`；QSS 用 `@token` 替换；`ensure_theme(app)` 幂等。
- 显示名改“便捷解压”：`ui/main_window.py:180`、`main.py:87/96`、`ui/main_window.py:176` docstring、README 标题。
- 生成/替换 `assets/app_icon.png`、`assets/app_icon.ico`；标题栏 16px 用 C；16px 简化版去掉 chevron。
- 先不重排页面；确认测试仍过、应用能启动。

### P1 壳 + 任务页（1–1.5 天）

- 40px 标题栏（P1 可先用原生，P3 再自绘）+ 260/48 导航 + `QStackedWidget`。
- 任务页：V2 渐变、拖放卡、任务卡、状态栏；保留 `_table` 等属性。
- 拖放统一到 Shell；`dropEvent` / `_on_drop_paths_ready` 兼容。
- 保留右键 4 项、双击定位、清空已完成、回收站开关。

### P2 设置页 V2（1.5–2 天，重点）

- `ui/pages/settings_page.py`：五组分组卡，24px 边距，64px 行。
- `SettingRow` / `SettingsGroup` / `NumberBox` / `ToggleSwitch`（继承 `QCheckBox`）。
- 自动保存 + 状态 + 校验 + 恢复默认 + 清空缓存。
- `SettingsDialog` 改为薄 wrapper；`_cb_delete_source` 属性、`_on_save()` flush。
- 同步任务页 `_cb_delete_source`；引擎/并发变化才 reload。
- 跑 UI 测试；确认 `_on_save` 在任何 dirty 状态下都写当前值。

### P3 密码库 + 对话框 + 响应式（1 天）

- 密码页 V2；`PasswordDialog` thin wrapper；3 列/表头/按钮代理。
- ContentDialog：密码输入/错误、导入预览、删除/清空确认、恢复默认/清缓存。
- 导航 1008 折叠；深色；高 DPI。
- 密码取消必须 `supply_password(None)`，不能留下等待线程。

### P4 打磨与验收（1–1.5 天）

- 焦点环、键盘、tooltip、空状态、禁用态、滚动条。
- 125/150/200% DPI；浅/深色截图；打包检查。
- 全量测试；手工验收矩阵。

工作量合计 5–7 天。不要一次性重写全部 UI；按阶段可回退。

---

## 6. 文件改动计划

新增：

```text
ui/theme/tokens.py
ui/theme/qss.py
ui/theme/theme_manager.py
ui/theme/icons.py
ui/widgets/title_bar.py
ui/widgets/navigation.py
ui/widgets/setting_row.py
ui/widgets/settings_group.py
ui/widgets/number_box.py
ui/widgets/toggle_switch.py
ui/widgets/content_dialog.py
ui/widgets/status_pill.py
ui/widgets/drop_card.py
ui/pages/tasks_page.py
ui/pages/passwords_page.py
ui/pages/settings_page.py
```

改写：

```text
ui/main_window.py       # 壳 + 页面 + 兼容属性
ui/settings_dialog.py   # thin wrapper
ui/password_dialog.py   # thin wrapper
main.py                 # 显示名/主题/图标
README.md               # 标题/说明
assets/app_icon.png
assets/app_icon.ico
```

不改：

```text
core/*      data/*      tests/*      ShellBreaker.spec
```

如果测试断言了旧标题“懒人解压”，先报告，不要擅自改测试；优先只改显示文案，测试断言应仍通过。

---

## 7. 测试与验收命令

基线（动手前先跑一次，记录结果）：

```powershell
python -m pytest -q
```

分阶段至少跑：

```powershell
python -m pytest tests/test_feature_updates.py -q
python -m pytest -q
```

启动检查：

```powershell
python main.py
```

注意：

- `tests/conftest.py` 设置 `QT_QPA_PLATFORM=offscreen`，字体可能缺失；**不要用 offscreen 截图验收视觉**。
- 需要截图时用 Windows 平台隐藏窗口 grab，并设 `QT_SCALE_FACTOR=2`；参考 `docs/mockups/fluent_prototype.py`。
- 测试会读写 `data/config.json`；测试前备份，测试后恢复，避免污染用户配置。
- 不要用 `QTest` 依赖真实屏幕；CI 是 offscreen。

手工验收重点：

1. 拖入 zip/rar/7z/伪装 `.mp4`/目录/非首卷/过小文件，状态与统计正确。
2. 双击成功任务定位；右键 4 项；清空已完成。
3. 设置页改并发/扫描/最小大小/回收站；自动保存状态；无效路径不写；恢复默认；清缓存。
4. 密码库添加/剪贴板/文件/删除/清空；密码错误重试；取消跳过。
5. 1008/1007 宽度导航切换；125/200% DPI；深色。
6. 显示名“便捷解压”；技术名/数据路径未变；图标 C 在 16/32/48/256 可辨。

---

## 8. 已知坑

| 坑 | 处理 |
|---|---|
| QSS 实色盖住渐变 | `Root/Content/Page/QStackedWidget/ScrollArea` 透明；`Shell.paintEvent` 画渐变 |
| 自绘标题栏 snap 异常 | P1 先用原生；P3 再做 `startSystemMove` / `nativeEvent`，保留回退 |
| `QCheckBox` 自绘 toggle 不发信号 | 继承 `QCheckBox`，不要用纯 QWidget 重写 |
| 设置页自动保存导致测试污染 | 测试前备份 config；`SettingsDialog._on_save()` 始终写当前值 |
| `max_workers` 每次步进重启线程池 | 400ms 防抖，只在引擎/并发变化时 reload |
| 表格状态文本被归一化 | 保留原始字符串；统计用前缀判断 |
| 拖放重复提交 | 只保留一个 drop handler |
| 主题只在 main.py 设置 | `MainWindow` + wrapper 都 `ensure_theme()` |
| 改名影响打包 | 只改显示字符串；`ShellBreaker.spec` 的 `name` 不动 |
| 16px 图标糊 | C 的 16px 单独简化；不要只缩 512 |

---

## 9. 交付报告格式

每个阶段结束报告：

```text
阶段：P?
改动文件：
- ...
测试：
- python -m pytest ... → 通过/失败（粘贴关键行）
截图/手工检查：
- ...
风险/未完成：
- ...
```

不要只说“已完成”；必须给出测试结果和手工检查点。遇到需要产品决策的，不要猜，回到设计会话确认。

---

## 10. 给新会话的提示词

见 [`docs/IMPLEMENTATION_PROMPT.md`](IMPLEMENTATION_PROMPT.md)。把该文件内容整段粘到新窗口即可。
