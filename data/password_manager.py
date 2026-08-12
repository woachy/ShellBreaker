"""
密码数据层 — 密码库的 JSON 持久化读写
严格遵循【铁律三】：ensure_ascii=False + utf-8 编码，完美兼容纯中文密码。
"""
import json
import logging
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger("ShellBreaker.PasswordManager")


class PasswordManager:
    """密码库管理器，提供 CRUD 操作并实时同步到本地 JSON 文件。"""

    def __init__(self):
        if getattr(sys, "frozen", False):
            self._file = Path(sys.executable).parent / "data" / "passwords.json"
        else:
            self._file = Path(__file__).resolve().parent / "passwords.json"
        self._cache: list[dict[str, str]] = []
        self._loaded = False

    # ---------- 内部方法 ----------

    def _ensure_loaded(self):
        """懒加载：首次访问时从 JSON 文件读取。"""
        if self._loaded:
            return
        self.reload()

    def reload(self):
        """强制从磁盘重新加载密码库。"""
        if not self._file.exists():
            self._cache = []
            self._loaded = True
            logger.debug("密码库文件不存在，初始化为空列表")
            return
        try:
            with open(self._file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, list):
                raise ValueError("JSON 顶层结构必须为列表")
            self._cache = raw
            self._loaded = True
            logger.info(f"密码库已加载，共 {len(self._cache)} 条记录")
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(f"密码库文件损坏 ({e})，降级为空列表")
            self._cache = []
            self._loaded = True

    def _persist(self):
        """将当前缓存写回 JSON 文件（铁律三：ensure_ascii=False + utf-8）。"""
        self._file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._file, "w", encoding="utf-8") as f:
            json.dump(self._cache, f, ensure_ascii=False, indent=2)
        logger.debug(f"密码库已持久化 ({len(self._cache)} 条)")

    # ---------- 公开 CRUD 接口 ----------

    def get_all(self) -> list[dict[str, str]]:
        """获取全部密码记录（返回副本，防止外部篡改缓存）。"""
        self._ensure_loaded()
        return [dict(item) for item in self._cache]

    def add(self, password: str, tag: str) -> int:
        """
        添加一条密码记录。
        返回新记录在列表中的索引。
        """
        self._ensure_loaded()
        record = {"password": password, "tag": tag}
        self._cache.append(record)
        self._persist()
        idx = len(self._cache) - 1
        logger.info(f"密码已添加 [索引={idx}]，标签: {tag}")
        return idx

    def delete(self, index: int) -> dict[str, str] | None:
        """
        按索引删除密码记录。
        返回被删除的记录；索引无效时返回 None。
        """
        self._ensure_loaded()
        if index < 0 or index >= len(self._cache):
            logger.warning(f"删除失败：索引 {index} 越界（总数 {len(self._cache)}）")
            return None
        removed = self._cache.pop(index)
        self._persist()
        logger.info(f"密码已删除 [索引={index}]，标签: {removed.get('tag')}")
        return removed

    def clear_all(self) -> int:
        """
        清空全部密码记录。
        返回被删除的记录总数。
        """
        self._ensure_loaded()
        count = len(self._cache)
        self._cache.clear()
        self._persist()
        logger.info(f"密码库已清空，共删除 {count} 条记录")
        return count

    @property
    def count(self) -> int:
        """当前密码库记录总数。"""
        self._ensure_loaded()
        return len(self._cache)