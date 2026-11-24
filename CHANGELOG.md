# Changelog

本文档记录 ShellBreaker 各正式版本的功能新增、优化调整与缺陷修复。

## [1.2.1] - 2026-11-24

### 修复
- **大体积媒体伪装包魔数误报与截断缺陷**：统一 `detect_embedded_archive` 为全链路魔数与图种探测唯一入口；在保留偏移 `0` 传统归档魔数宽容识别的同时，对偏移 `> 0` 的 7z 增加 32B StartHeader CRC32 及边界校验、对 RAR4/RAR5 增加主头/块头结构与 CRC 强校验；优先通过 O(1) 尾部 ZIP/ZIP64 EOCD 反算真实起始偏移并修复窗口偏移 `0` 边界漏检；取消非媒体文件的 64MB 体积截断限制，避免 2GB+ MP4 等大载体文件因随机字节碰撞被误判为 7z/RAR。
- **伪装来源回退链②短路问题**：当源文件本身为伪装后缀（`is_disguised_source=True`）且第一阶段改名尝试非密码失败时，不再因扩展名非 `.zip` 而短路链②，改为优先通过同盘硬链接（跨盘回退复制）临时改名 `.zip` 重试，且清理阶段仅删除沙箱内临时硬链接，不触碰原始文件。
- **Bandizip GUI 路径误用导致秒退**：抽取并统一 `resolve_bandizip_cli` 解析函数，在 `BandizipEngine`、`discover_engine` 与 `SettingsPage` 三处自动将误选的 `Bandizip.exe`（GUI 程序）纠偏为同目录命令行程序 `bz.exe` / `bc.exe`，缺失 CLI 时拒绝调用 GUI 并降级处理。
- **WinRAR 对非支持格式盲扫修复与加密 ZIP 退出码误判**：新增 `BaseEngine.supports_repair_for` 守卫，拦截 WinRAR `r` 命令对 `.7z` 等不支持格式的长时间盲扫修复，并在执行修复前上报 `-1` 不确定脉冲进度态；针对实机 WinRAR 解压加密 ZIP 统一返回 `exit=10` 的行为，结合 O(1) `_is_encrypted_zip` 判定按是否传密精准分流 `PasswordRequiredError` 与 `WrongPasswordError`，同时维持坏包直抛 `ExtractionFailedError`。
- **三引擎快速预检与偏移图种适配**：统一 Bandizip、WinRAR 与 7-Zip 快速验密预检路径，适配带非零偏移的图种归档探测与提取。

### 优化
- **回归测试与工程规范沉淀**：补充 `tests/test_v121_bugfixes.py` 专项回归测试套件覆盖上述魔数校验、伪装链②硬链接回退、Bandizip CLI 纠偏与 WinRAR 退出码分流场景，并新增项目专属工程开发技能文档 `.agents/skills/shellbreaker-dev/SKILL.md`。

## [1.2.0] - 2026-11-21

### 新增
- **独立解压进度列与绿格折叠组件 (`ExtractionProgressWidget`)**：任务表拆分为 5 列，独立「解压进度」列支持按已完成嵌套层数绘制绿色进度格（超出 `max_green_cells` 后自动折叠为 `1 格 + × N`），右侧同步展示当前层进度条、不确定态动画与终态状态标签。
- **双计数器与反复解压阈值暂停确认**：在 `ExtractionPipeline` 中拆分单层计数与累计子包计数；当单任务累计子包解压达到 `nested_confirm_limit` 且开启 `enable_nested_limit_confirm` 时暂停任务并弹出 `NestedLimitConfirmDialog`，由用户选择继续、停止并保留已解压成果或取消并清理沙箱。
- **内置 7-Zip 兜底内核 (`SevenZipEngine`)**：集成 `assets/bin/7z.exe` 与 `7z.dll`（附 `License.txt`），支持 `-bsp1` 实时进度百分比解析、`-slt` 加密头/最小成员快速验密，以及未安装 Bandizip / WinRAR 时的自动兜底发现。
- **图种偏移嗅探与剥离提取**：新增 `detect_embedded_archive` 魔数偏移嗅探（支持 JPEG/PNG/GIF/BMP/WebP/MP4/MKV 等载体中内嵌 ZIP/RAR/7Z/GZIP/XZ/ZSTD 载荷、跨缓冲区边界扫描及 ZIP 结构校验防误报），管线支持剥离媒体头部后独立提取。
- **同盘隐藏沙箱与重命名事务恢复**：在目标输出目录同盘建立 `.sb_tmp_<task_id>` 隐藏工作区实现零拷贝交付，并引入重命名事务日志（`record_rename_journal` / `recover_interrupted_renames`），在异常中断重启后自动恢复被临时改名的原始文件。
- **上下文密码嗅探与快速预检**：新增 `extract_contextual_passwords` 与 `read_text_safe`（编码自适应解码），可从文件名与同目录文本中提取候选密码；批量尝密前先通过引擎预检接口快速验证，减少大包全量试错耗时。
- **分卷连续性预检 (`check_volume_completeness`)**：任务入队与解压前校验 `.partN.rar`、`.7z.001`、`.z01/.zip` 等分卷序列完整性，缺失分卷时阻断任务并提示具体缺失卷。
- **源文件清理三模式与推广文件过滤**：支持 `keep`、`recycle_bin`、`permanent` 三种源文件清理模式（保持对旧版 `delete_source_after_extract` 键的向后兼容）；新增交付前推广/广告垃圾文件过滤（`enable_ad_filter` 与 `ad_filter_patterns`）。
- **监听文件夹与单实例 IPC (`core/system_integration.py`)**：新增 `SingleInstanceBridge`（基于 `QLocalServer` / `QLocalSocket` 转发二次启动传入路径）与 `WatchFolderService`（基于 `QFileSystemWatcher` 监测目录、忽略下载临时后缀并在文件稳定后自动入队）。
- **浅色 / 深色主题动态切换**：新增 `ThemeManager` 主题切换与 `theme` 配置项，支持在设置页实时切换浅色与深色主题并即时刷新界面样式。

### 优化
- **合并最小文件体积配置**：将原「最小解压文件大小」与「伪装文件最小探测体积」合并为统一配置项 `min_file_size_mb`，支持设置为 `0` 表示不限制，并移除冗余的 `disguise_min_size_mb`。
- **精简系统集成能力**：移除「Windows 资源管理器右键菜单」开关与注册表读写逻辑，仅保留监听目录自动解压与单实例 IPC。
- **引擎进度与进程管理增强**：`BaseEngine` 支持流式进度回调与沙箱体积旁路采样估算；原子写盘改造覆盖 `ConfigManager` 与 `PasswordManager`，避免临时文件残留。

### 修复
- **原生弹窗黑底缺陷**：将 `ui/theme/qss.py` 中全局 `QDialog { background: transparent; }` 改为按 `[framelessDialog="true"]` 属性选择器隔离无边框卡片弹窗，并为 `QMessageBox`、`QInputDialog` 等原生弹窗补充高对比度样式，修复在 Windows 浅/深色主题下背景显示纯黑的问题。
- **终态备注被覆盖**：修复任务进入成功/失败/跳过等终态后，任务表备注列被迟到进度消息或「任务结束」文本覆盖的问题。
- **目录扫描源路径缺失**：修复 `TaskQueue.scan_directory()` 入队任务未向队列及主窗口登记 `src_path` 的问题。
- **非密码错误误跑密码库**：修复文件损坏等非密码类 `ExtractionFailedError` 仍遍历密码库并闪现「验证密码」状态的问题。
- **WinRAR 修复目录污染源目录**：修复 `WinRAREngine.repair_extract` 在源压缩包所在目录生成 `_repaired` 文件夹的问题，改为使用系统临时目录并在完成后回收。
- **嵌套分卷与队列退出/重载问题**：修复嵌套层 `.001` 与分卷次卷未被跳过清理的问题，以及 `BaseEngine.terminate_all()` / `TaskQueue.abort_all()` / `TaskQueue.reload_config()` 在中止或调整并发数时未清理活跃子进程或丢弃排队任务的问题。
- **体积阈值 `0` 值读取缺陷**：修复 `SettingsPage.reload_from_config` 与管线中使用 `or 50` 导致 `min_file_size_mb = 0` 被误覆盖为 `50` 的问题。

## [1.1.1] - 2026-11-10
- **新增**：支持原生文件夹（非压缩包）本体熔断保护；新增任务右键「打开源路径」与「中止任务」操作。
- **修复**：修复任务状态流转异常问题，同步更新文档说明。

## [1.1.0] - 2026-10-20
- **优化**：应用界面采用 Fluent 风格视觉与交互重构，显示名称升级为「便捷解压」。
- **改进**：补充 GitHub Actions CI 解压引擎安装与 Qt `offscreen` 测试环境配置。

## [1.0.6] - 2026-10-15
- **修复**：修复主窗口全局拖拽引发的 `NameError` 崩溃与多线程并发安全隐患；规范化 `ShellBreaker.spec` 打包配置并解耦解压管线。

## [1.0.5] - 2026-10-08
- **新增**：支持解压成功后将源文件及同族分卷移入回收站，任务入队自动过滤非首卷。
- **新增**：密码库记录使用频次与时间戳（高频密码优先尝试），支持按密码文本精确删除及从剪贴板/TXT 批量导入（含前缀清洗与预览弹窗）。

## [1.0.4] - 2026-10-03
- **优化**：将第一层解压容错逻辑平移至中间层伪装包处理；支持基础图种解压，完善核心回滚与白名单保护。

## [1.0.3] - 2026-10-03
- **新增**：强化嵌套层形态驱动伪装检测、受保护终点白名单（如 APK 等）与零拷贝解压交付。

## [1.0.2] - 2026-09-29
- **优化**：优化嵌套解压为两层结构，修复 WinRAR 静默解压与界面交互细节；补齐测试输出路径隔离并精简 `README.md`。

## [1.0.1] - 2026-08-12
- **修复**：修复 PyInstaller 单文件打包模式下资源路径解析异常导致的样式与图标无法加载问题。
