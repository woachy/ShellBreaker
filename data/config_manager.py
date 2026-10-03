"""
全局配置管理器 — 读写 data/config.json。
阶段 4.5 新增：供全局设置面板双向同步。
"""
import json
import logging
import sys
from pathlib import Path
from typing import Any

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
    "min_file_size_mb": 50,            # 最小解压文件大小 (MB)，小于此值跳过
    "disguise_min_size_mb": 10,        # 嵌套层伪装包最小识别大小 (MB)，默认 10MB
    "bandizip_path": "",               # bz.exe 绝对路径，空字符串=自动扫描
    "winrar_path": "",                 # WinRAR.exe 绝对路径，空字符串=自动扫描
}


class ConfigManager:
    """全局配置单例管理器。"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._data: dict[str, Any] = {}
            cls._instance._loaded = False
        return cls._instance

    def _ensure_loaded(self):
        if self._loaded:
            return
        if CONFIG_PATH.exists():
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
                # 填充缺失的默认键
                for key, val in DEFAULT_CONFIG.items():
                    if key not in self._data:
                        self._data[key] = val
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(f"config.json 损坏 ({e})，使用默认配置")
                self._data = dict(DEFAULT_CONFIG)
        else:
            self._data = dict(DEFAULT_CONFIG)
            self.save()
        self._loaded = True
        logger.debug(f"配置已加载: {self._data}")

    def save(self):
        """持久化到磁盘（utf-8 + indent）。"""
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)
        logger.info(f"配置已保存: {dict(self._data)}")

    def get(self, key: str, default: Any = None) -> Any:
        self._ensure_loaded()
        return self._data.get(key, default)

    def set(self, key: str, value: Any):
        self._ensure_loaded()
        self._data[key] = value

    def get_all(self) -> dict[str, Any]:
        self._ensure_loaded()
        return dict(self._data)

    def reset_defaults(self):
        self._data = dict(DEFAULT_CONFIG)
        self.save()