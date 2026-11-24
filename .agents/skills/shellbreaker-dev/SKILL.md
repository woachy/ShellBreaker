---
name: shellbreaker-dev
description: >-
  ShellBreaker（便捷解压）项目专属工程架构指南、核心业务铁律、三大解压引擎避坑手册、UI/QSS 红线与打包发版 SOP。
  在 ShellBreaker 仓库（e:\ShellBreaker_Project_2）中进行新需求规划、Bug 排查、底层改造、UI 调整、回归测试或打包发版时必须激活。
---

# ShellBreaker（便捷解压）项目专属开发指南

> 建议与全局技能 `dev-orchestrator-prefs` 配合使用：主窗口只做架构决策与把关，重读/编码/审计/打包/发布统一交给 **Gemini 4 Neon (High)** 或 **Gemini 4 Hydrogen (High)** 子代理执行。

## 1. 技术栈与核心模块地图

- **运行环境**：Windows + Python 3.10+ + **100% 纯 PySide6**（自研 V2 Fluent 令牌与控件体系，**严禁引入 `qfluentwidgets`**）。
- **版本与留痕**：唯一版本号定义在 `pyproject.toml`，版本历史记录在根目录 `CHANGELOG.md`（发版同步维护，文案冷静客观准确）。
- **核心文件职责**：
  - `core/sandbox.py`：同盘隐藏沙箱（`.shellbreaker_sandbox/<task_id>`，无权回退 `SANDBOX_ROOT`，清理自动回收空容器）、零拷贝 `os.replace` 交付、伪装重命名事务日志（`logs/rename_journal.json` + 启动 PID 存活自愈）、`robust_rmtree`（清只读位 + 指数退避）、分卷组与断号预检 `check_volume_completeness`、**统一魔数/图种探测唯一入口 `detect_embedded_archive` / `detect_archive_type`**。
  - `core/engine.py`：`BaseEngine`（无缓冲流式双线程进度读取 + 沙箱体积旁路采样 + `terminate_all` + 探针缓存 + `supports_repair_for`）、`BandizipEngine`、`WinRAREngine`、`SevenZipEngine`（内置 `assets/bin/` 兜底）、统一 `resolve_bandizip_cli` 与 `discover_engine`。
  - `core/pipeline.py`：`ExtractionPipeline` 四级降维试错链（① 盲解 → ② 伪装来源同盘硬链接 `.zip` → ③ `supports_repair_for` 守卫修复 → ④ 已校验合法偏移的图种切片剥离）、双计数器（`total` 驱动绿格全程累加、`cycle` 触发嵌套暂停三态确认）、上下文密码置顶与大包多密快速预检、推广垃圾文件过滤 `filter_ad_files`、删除源文件三模式。
  - `core/task_queue.py`：多线程调度、7 个 Qt Signal（`task_status_changed`, `task_progress`, `task_registered`, `task_layer_progress`, `nested_limit_reached`, `password_needed`, `password_wrong`）、入队分卷连续性预检、跨线程 `threading.Event` 密码/嵌套确认同步、`abort_all`。
  - `core/system_integration.py`：单实例本地 IPC（`SingleInstanceBridge`，`QLocalServer`）与监听文件夹自动破壳（`WatchFolderService`，3s 稳定检测）。**注意：Windows 资源管理器右键菜单功能已按用户要求彻底移除，切勿擅自加回。**
  - `data/config_manager.py`：19 个配置项 + 原子写盘 + `delete_source_mode`（`keep`/`recycle_bin`/`permanent`）与旧键 `delete_source_after_extract` 双向同步。
  - `data/password_manager.py`：频次自学习排序 + 原子写盘 + `read_text_safe`（PUA 惩罚 + GB18030 同分决胜）+ 四级上下文密码嗅探 `extract_contextual_passwords` + `record_success` 自动入库。
  - `ui/`：`TasksPage` 5 列结构（`COL_FILENAME=0, COL_STATUS=1, COL_PROGRESS=2, COL_ENGINE=3, COL_REMARK=4`）、`ExtractionProgressWidget`（蓝条 + 绿格折叠 `🟩 × N` + `-1` 脉冲动画）、`StatusPillDelegate`（支持 `"待确认继续"` 与 `"失败(缺失分卷: ...)"`）、`SettingsPage`、`ThemeManager`（`V2_LIGHT`/`V2_DARK` 运行时动态切换 + DWM 深色标题栏）。

## 2. 不可违背的核心业务铁律（血泪经验防回归）

> **⚠️ 底层业务改动强制红线（编译通过 + 测试通过 ≠ 没有问题）**
> 凡新增或改动涉及较底层且影响业务功能的代码（`core/sandbox.py`、`core/engine.py`、`core/pipeline.py`、`core/task_queue.py`、`data/`、`ui/theme/qss.py` 等），**绝不能以“编译通过、pytest 全绿”作为无 Bug 的依据**。新旧代码之间极易产生“单测不报错、真实场景走错分支”的**隐性业务逻辑冲突**（历史真实教训：如 `detect_embedded_archive` 误改后缀短路旧版链②、`WinRAR` 加密 ZIP 错密 `exit=10` 击穿密码重试链、全局 `QDialog` 透明污染原生 `QMessageBox`）。
> - **动代码前**：必须通读上下游调用链，核验新设计是否与既有业务逻辑、容错兜底链和边界前提完全相符；
> - **动代码后**：必须逐条对照下方 A~D 四组铁律做 `git diff` 级独立逻辑相性审计，并对受影响的真实引擎/真实文件结构跑实机端到端冒烟，严禁只靠 Mock 测试签核。

### A. 魔数探测与图种识别红线（`core/sandbox.py`）
1. **终点白名单绝对最先拦截**：`PROTECTED_ENDPOINT_EXTS`（`.apk`, `.exe`, `.msi`）必须在 `detect_embedded_archive` 入口第一时间返回 `(None, -1)`——`.apk` 本质是带完整 EOCD 的合法 ZIP，绝不可被 ZIP EOCD 逻辑越过白名单误拆。
2. **偏移 `0` 宽容 vs 偏移 `> 0` 强校验**：
   - 偏移 `0` 的传统魔数（`.7z`/`.rar`/`.zip`/`.gz`/`.bz2`/`.xz`）保持宽容识别，避免误杀头轻微受损、正待修复链救回的归档；
   - 偏移 `> 0` 的媒体载体（JPEG/PNG/GIF/BMP/WebP/MP4/MOV）扫描，必须**优先执行 O(1) 尾部 ZIP/ZIP64 EOCD 反算真实起始偏移**（彻底避开大文件扫描盲区，反向扫描下界用 `>= 4` 防窗口偏移 `0` 漏检）；对 7z 必须校 32B StartHeader CRC32 与边界，对 RAR4/RAR5 必须校主头/块头结构与 CRC，**单点校验失败必须 `pos += 1` 继续扫**，杜绝 2GB+ MP4 随机字节碰撞误报。
3. **单一真相源**：`detect_archive_type`、`prepare_file`、`SevenZipEngine`、`pipeline` 全部复用 `detect_embedded_archive`，不得各自写第二套探测逻辑。

### B. 降维试错链与容错红线（`core/pipeline.py`）
1. **伪装包链②绝不短路 + 零拷贝硬链接**：当外层源文件本身是伪装后缀（`is_disguised_source=True`）时，即使 `prepare_file` 改成了非 `.zip` 扩展名，链①非密码失败后链②必须允许通过**同盘 `os.link` 硬链接**（跨盘才回退 `shutil.copy2`）改名 `.zip` 再试，绝不对 GB 级文件盲拷贝；`cleanup_files` 仅删除沙箱内临时硬链接，绝不伤及用户原始文件。
2. **嵌套层防过度穿透**：严守 `_get_native_folders()` 原生文件夹熔断保护与单孤儿伪装探测前提；嵌套分卷跳过 `is_secondary_volume` 并在首卷成功后清理沙箱同族分卷。
3. **双计数器严格口径**：密码错试、快速预检、伪装试探失败回滚、降级链失败均**严禁计入** `total`/`cycle`；仅在一次有效解压真实产出内容后各 `+1`。达到 `nested_confirm_limit` 时挂起而非销毁沙箱，支持 `continue`（仅清 `cycle`）、`stop_keep`（保留交付）、`cancel`（销毁回滚）。
4. **推广过滤安全底线**：`filter_ad_files` 仅在目录内仍保留 `>= 1` 个非广告主业务文件时才执行删除，绝不清空整个产物目录。

### C. 三大解压引擎实机避坑清单（`core/engine.py`）
1. **Bandizip**：
   - `Bandizip.exe` 是 GUI 程序，传 CLI 参数会无输出秒退 `exit=1`！
   - `BandizipEngine.__init__`、`discover_engine`、`SettingsPage`（选择、加载回显、`flush`）**三处必须共用 `resolve_bandizip_cli()`** 自动纠偏为同目录 `bz.exe`/`bc.exe`，无 CLI 时拒绝调用 GUI 并优雅降级/提示。
2. **WinRAR**：
   - **加密 ZIP 退出码 `10` 陷阱**：实机 `WinRAR.exe x -ibck -inul -y -p...` 解压加密 `.zip`（无论无密 `-p-` 还是错密 `-pWrong`）均返回 **`exit=10`** 而非 `11`！必须结合 O(1) `_is_encrypted_zip` 按 `pw_provided` 分流抛 `WrongPasswordError` / `PasswordRequiredError`，同时保持坏包抛 `ExtractionFailedError`（严守 Bug #3 防坏包盲跑密码库）；
   - **`repair_extract` 守卫**：WinRAR `r` 仅支持 `.rar`/`.zip`，必须经 `supports_repair_for` 拦截 `.7z` 等格式（否则对 2GB+ 文件盲扫 60s+ 卡 0%）；执行 `r` 前推送 `on_progress(-1)` 脉冲态；修复目录放系统临时目录并在 `finally` 清理（绝不污染源目录）；无修复产物时保留加密头 RAR 密码判断，其余直接失败不再重复跑 `extract`；
   - `.rar` 优先同目录 `UnRAR.exe` 获取实时百分比（注意：`UnRAR.exe` 不支持 `-ibck` 且不可传 `-inul`）。
3. **7-Zip（内置兜底）**：
   - 二进制位于 `assets/bin/`（`7z.exe` + `7z.dll` + `7za.exe` + `License.txt`），优先 `7z.exe + 7z.dll` 全格式组合；
   - `-bsp1` 原生输出实时进度；`supports_repair_for` 返回 `False` 避免链③重复执行；`extract` 与 `test_password` 均适配偏移图种 `-t<type>`。

### D. UI / QSS / 配置兼容红线（`ui/` & `data/`）
1. **原生弹窗黑底禁令**：`ui/theme/qss.py` 中绝不可对全局 `QDialog` 设置 `background: transparent;`（Windows 下无 `WA_TranslucentBackground` 的 `QMessageBox`/`QInputDialog` 会变纯黑 `#000000`）！仅允许对 `QDialog[framelessDialog="true"]`（`ContentDialog` 体系）设置透明底，并为 `QMessageBox`/`QInputDialog` 保持浅/深色显式底色与文字对比度。
2. **单一最小体积阈值**：设置页只保留单一的「最小解压文件大小（`min_file_size_mb`，下限 `0` 表示不限制）」，同时覆盖文件夹扫描与嵌套伪装启发式探测（已确认图种与真实嵌套包不受限）；读取配置时严禁用 `or 50` 误吞合法值 `0`，也不可重新拆出冗余的 `disguise_min_size_mb`。
3. **终态备注防覆写**：任务进入终态（`"成功"` / `"失败*"` / `"已中止"` / `"已跳过*"`）后，严禁再被进度回调覆写 `COL_REMARK`。
4. **旧测试兼容层**：保留 `ui/password_dialog.py`、`ui/settings_dialog.py`、`SettingsPage.cb_delete_source` 同步兼容属性、`TaskQueue` 末尾兼容别名与 `MainWindow` 隐藏测试属性。

## 3. 测试、排障、打包与发布 SOP

### 1. 自动化回归测试
```powershell
$env:QT_QPA_PLATFORM="offscreen"; pytest -v --durations=15 -o log_cli=false
```
- 当前基线：**121 passed**（7 个测试文件：`test_disguise_and_whitelist.py`, `test_feature_updates.py`, `test_nesting_layers.py`, `test_winrar_engine.py`, `test_phase0_phase1.py`, `test_phase2.py`, `test_phase3.py`, `test_v121_bugfixes.py`）。
- 若前次异常中断导致 Windows `C:\Temp\pytest-of-*` 残留 `.lock` 引起 `tmp_path` 卡慢，先清理锁文件再复跑。
- 新增单测必须隔离临时 `config.json`/`passwords.json`/`SANDBOX_ROOT`/`rename_journal.json`，严禁污染工作区真实配置或 Windows 注册表；`tests/conftest.py` 的 GUI 主线程 GC fixture 需保持生效。

### 2. 线上日志排障入口
- 运行日志在程序同级 `logs/app.log`（`RotatingFileHandler`，5MB×3）。
- 收到用户反馈的 `app.log` 时，优先只读排查：魔数探测后缀与偏移、沙箱路径、实际调用的引擎 exe 路径（警惕 `Bandizip.exe`）、完整命令行参数、退出码与降级链跳转，再对照代码找根因。

### 3. 单独打包（PyInstaller）
```powershell
python -m PyInstaller --clean --noconfirm ShellBreaker.spec
```
- 注意：`pyinstaller` 命令不在本机 `PATH` 中，必须使用 `python -m PyInstaller`；输出单文件 `dist\ShellBreaker.exe`（自动打包 `assets/bin/`）。

### 4. 正式发版流程（仅在用户明确要求发版时执行）
1. 递增 `pyproject.toml` 版本号并按冷静客观风格追加 `CHANGELOG.md`；
2. 跑通全仓 `pytest -v`（0 failed）并执行 `python -m PyInstaller --clean --noconfirm ShellBreaker.spec`；
3. 生成 `dist\ShellBreaker-vX.Y.Z.zip`（内含 `ShellBreaker.exe`），计算 `.exe` 与 `.zip` 的 SHA256；
4. 确认无运行中进程后，用 `dist\ShellBreaker.exe` 覆盖桌面快捷方式指向的 **`F:\gametool\ShellBreaker-v1.0.0\ShellBreaker.exe`**（校验 SHA256 一致，**严禁触碰同目录 `data\` 与 `logs\`**）；
5. Git 提交至 `main` 并打 annotated tag `vX.Y.Z`；配合 `github-token-access` 技能推送至 `https://github.com/woachy/ShellBreaker.git`，创建 GitHub Release 并同时上传 `ShellBreaker-vX.Y.Z.zip` 与 `ShellBreaker.exe`（严禁回显 PAT 明文）。
