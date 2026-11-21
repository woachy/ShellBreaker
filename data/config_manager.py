"""
全局配置管理器 — 读写 data/config.json。
阶段 4.5 新增：供全局设置面板双向同步。
"""
import copy
import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger("ShellBreaker.ConfigManager")

if getattr(sys, "frozen", False):
    CONFIG_PATH = Path(sys.executable).parent / "data" / "config.json"
else:
    CONFIG_PATH = Path(__file__).resolve().parent / "config.json"

DEFAULT_CONFIG = {
    "default_engine": "bandizip",      # "bandizip" | "winrar" — 默认解压引擎偏好
    "output_mode": "source_directory", # "source_directory" | "custom"
    "custom_output_path": "",
    "max_workers": 2,
    "max_scan_depth": 4,               # 拖入文件夹时的最大递归扫描层数 (1-50)
    "min_file_size_mb": 50,            # 最小解压文件大小 (MB)，小于此值跳过（0 表示不限制）
    "bandizip_path": "",               # bz.exe 绝对路径，空字符串=自动扫描
    "winrar_path": "",                 # WinRAR.exe 绝对路径，空字符串=自动扫描
    "delete_source_after_extract": False, # 解压成功后自动删除源文件（移至回收站）
    "enable_nested_limit_confirm": True,  # 嵌套解压超过阈值时是否弹窗确认
    "nested_confirm_limit": 10,           # 嵌套解压触发确认的子包数量阈值
    "max_green_cells": 5,                 # 成功状态最多展示的绿格数量
    "sevenzip_path": "",                  # 自定义 7z.exe / 7za.exe 路径，空字符串=优先自动发现系统 7-Zip 或内置 assets/bin/
    "delete_source_mode": "keep",         # 源文件清理模式: "keep" | "recycle_bin" | "permanent"
    "enable_ad_filter": True,             # 是否在交付前自动清理推广/广告垃圾文件
    "ad_filter_patterns": [
        "*.url",
        "*扫码关注*",
        "*资源发布页*",
        "*最新地址*",
        "*免责声明*.txt",
        "*解压必看*.url",
    ],
    "enable_watch_folder": False,         # 是否开启监听文件夹自动解压
    "watch_folder_path": "",              # 监听目录路径
    "theme": "light",                     # 界面主题: "light" | "dark"
}


class ConfigManager:
    """全局配置单例管理器。"""

    _instance = None
    _lock = threading.RLock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._data: dict[str, Any] = {}
                cls._instance._loaded = False
        return cls._instance

    def _ensure_loaded(self):
        with self._lock:
            if self._loaded:
                return
            self.load()

    def load(self):
        """从磁盘加载配置（缺失则写入默认配置），并同步新旧删除模式键。"""
        with self._lock:
            if CONFIG_PATH.exists():
                try:
                    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                        loaded = json.load(f)
                    if not isinstance(loaded, dict):
                        raise ValueError("config.json 顶层必须为 JSON 对象")
                    has_delete_mode = "delete_source_mode" in loaded
                    self._data = dict(loaded)
                    # 填充缺失的默认键
                    for key, val in DEFAULT_CONFIG.items():
                        if key not in self._data:
                            self._data[key] = copy.deepcopy(val)
                    # 向后兼容同步：delete_source_mode <-> delete_source_after_extract
                    if not has_delete_mode:
                        if bool(self._data.get("delete_source_after_extract", False)):
                            self._data["delete_source_mode"] = "recycle_bin"
                    else:
                        self._data["delete_source_after_extract"] = (
                            self._data["delete_source_mode"] != "keep"
                        )
                except (json.JSONDecodeError, Exception) as e:
                    logger.warning(f"config.json 损坏 ({e})，使用默认配置")
                    self._data = copy.deepcopy(DEFAULT_CONFIG)
            else:
                self._data = copy.deepcopy(DEFAULT_CONFIG)
                self.save()
            self._loaded = True
            logger.debug(f"配置已加载: {self._data}")

    def save(self):
        """持久化到磁盘（utf-8 + indent，同目录临时文件 + os.replace 原子写盘）。"""
        with self._lock:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=CONFIG_PATH.parent,
                    prefix=f"{CONFIG_PATH.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as f:
                    tmp_path = Path(f.name)
                    json.dump(self._data, f, indent=2, ensure_ascii=False)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, CONFIG_PATH)
            except Exception:
                if tmp_path is not None and tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                raise
            logger.debug(f"配置已保存: {dict(self._data)}")

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            self._ensure_loaded()
            val = self._data.get(key, default)
            return copy.deepcopy(val) if isinstance(val, (list, dict)) else val

    def _sync_delete_mode(self, key: str, value: Any):
        if key == "delete_source_mode":
            self._data["delete_source_mode"] = value
            self._data["delete_source_after_extract"] = (value != "keep")
        elif key == "delete_source_after_extract":
            bool_val = bool(value)
            self._data["delete_source_after_extract"] = bool_val
            if bool_val:
                if self._data.get("delete_source_mode") != "permanent":
                    self._data["delete_source_mode"] = "recycle_bin"
            else:
                self._data["delete_source_mode"] = "keep"
        else:
            self._data[key] = value

    def set(self, key: str, value: Any):
        with self._lock:
            self._ensure_loaded()
            self._sync_delete_mode(key, value)

    def update(self, mapping: Mapping[str, Any]):
        """批量更新配置键值，并保持 delete_source_mode 与旧键双向同步。"""
        with self._lock:
            self._ensure_loaded()
            for k, v in mapping.items():
                if k not in ("delete_source_mode", "delete_source_after_extract"):
                    self._data[k] = v
            if "delete_source_mode" in mapping:
                self._sync_delete_mode("delete_source_mode", mapping["delete_source_mode"])
            elif "delete_source_after_extract" in mapping:
                self._sync_delete_mode(
                    "delete_source_after_extract",
                    mapping["delete_source_after_extract"],
                )

    def get_all(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_loaded()
            return copy.deepcopy(self._data)

    def reset_defaults(self):
        with self._lock:
            self._data = copy.deepcopy(DEFAULT_CONFIG)
            self.save()