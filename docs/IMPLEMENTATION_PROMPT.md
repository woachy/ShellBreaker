# 新会话实施提示词（复制整段）

```text
你是在 E:\ShellBreaker_Project_2 上实施“便捷解压”V2 UI 重写的编码代理。用户已经确认：图标 C（层叠剥离，整体下移 7%）、设置页自动保存、只改中文显示名“便捷解压”（技术名 ShellBreaker / EXE / data 路径不动）。本轮只做实施，不再重新设计。

当前工作区已有一版 Gemini 3.8 Flash 生成的 UI：python -m pytest -q 为 42 passed，但视觉审计失败（任务表头被空状态层盖住、设置页宽度塌缩、原生 QSpinBox 上下键不可靠、QComboBox 无箭头、仍是原生标题栏、字体/控件质感不统一）。不要在这个 UI 上逐项修补，读完审计后按“UI 表现层重写、业务层保留”执行。

开始前按顺序读：
1. docs/UI_REFACTOR_PLAN_V2.md
2. docs/IMPLEMENTATION_HANDOFF.md（重点 §1.1 当前实现审计）
3. docs/UI_REFACTOR_PLAN.md 第 7、8 节
4. tests/test_feature_updates.py:585-746
5. ui/main_window.py、ui/settings_dialog.py、ui/password_dialog.py、main.py
6. data/config_manager.py、core/task_queue.py

硬约束：
- 不改 core/、data/ 的公开接口和业务逻辑；不改 tests/ 的断言；不改 ShellBreaker.spec 的 name 和 data 路径。
- 只改显示名；技术包名/脚本/EXE/数据目录保持 ShellBreaker。
- 必须保留测试依赖的属性：MainWindow._table/_task_rows/_task_data/_cb_delete_source/_on_task_status_changed/dropEvent/_on_drop_paths_ready；SettingsDialog._cb_delete_source/_on_save；PasswordDialog._table（3 列，表头精确“密码（明文）/使用次数/标签”）/_btn_add/_btn_import_clip/_btn_import_file/_btn_delete/_btn_clear/_refresh_table。
- 控件构造后立即可用，不能懒加载。ToggleSwitch 必须继承 QCheckBox，setChecked/toggled 正常。
- SettingsDialog._on_save() 无论 dirty 与否都写当前控件值。
- 状态文本原样保留，不归一化“已跳过/已跳过(非首卷分卷)/已跳过(文件过小)”。
- 主题在 MainWindow 和两个 wrapper 里 ensure_theme()，不能只在 main.py 设置。
- 拖放只保留一个 handler，避免重复提交。
- 不要直接复制 docs/mockups/fluent_prototype.py 进 ui/；它是静态设计稿。

执行策略：
1. 先建分支 ui/v2-reset，保留当前实现可回退。
2. 保留 core/data/tests、MainWindow 业务方法、两个 wrapper、信号和兼容属性；重写 ui/theme/、ui/widgets/、ui/pages/、MainWindow 壳。
3. 先做“40px 标题栏 + 任务页”可运行视觉 spike，截图对 docs/mockups/out/v2_tasks_light.png；确认方向后再铺设置页/密码页。
4. 不要保留当前坏布局；只复用业务逻辑和自动保存/校验逻辑。

实施顺序：
P0 token/主题/图标 C/显示名 → 跑测试；
P1 壳 + 任务页 → 跑测试；
P2 设置页分组 + 自动保存 + 兼容 wrapper → 跑测试；
P3 密码页/对话框/1008 响应式/深色 → 跑测试；
P4 DPI/焦点/空状态/打包检查。
每个阶段先给 5–10 行计划，再改代码；每阶段结束报告改动文件、测试命令与结果、手工检查点、风险。

测试：
- 动手前先跑 python -m pytest -q 记录基线（当前基线 42 passed）。
- 每阶段至少跑 python -m pytest tests/test_feature_updates.py -q 和 python -m pytest -q。
- 不要用 offscreen 截图做视觉验收；需要截图时用 Windows 平台隐藏窗口 grab + QT_SCALE_FACTOR=2。

如果代码/测试与规划冲突，停下来报告，不要猜，也不要擅自改测试或扩范围。先读文件再改，不要凭空虚构接口。
```
