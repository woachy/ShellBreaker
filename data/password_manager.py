"""
密码数据层 — 密码库的 JSON 持久化读写、使用频率自学习与批量导入解析。
严格遵循【铁律三】：ensure_ascii=False + utf-8 编码，完美兼容纯中文密码。
"""
import json
import logging
import re
import sys
import time
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("ShellBreaker.PasswordManager")

# 密码文本提取前缀与内嵌正则
PREFIX_PATTERN = re.compile(
    r"^[【\[]?\s*(?:解压密码|解压码|提取码|密码|password|pwd)\s*[】\]]?\s*[:：=\s]\s*",
    re.IGNORECASE,
)
INLINE_PATTERN = re.compile(
    r"(?:解压密码|解压码|密码|pwd)\s*[:：=]\s*(\S+)",
    re.IGNORECASE,
)


QUOTE_CHARS = "\"'“”‘’【】[]《》<>()（）"


def parse_passwords_text(raw_text: str) -> list[str]:
    """提取有效密码候选词，过滤注释、空行、超长乱码，剥离前后缀与包装符号。"""
    candidates = []
    seen = set()
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(("#", "//", ";")):
            continue

        cleaned = None
        # 1. 优先尝试抽取内嵌密码（无论整行长短，如 '论坛专享 密码: 123456'）
        m = INLINE_PATTERN.search(line)
        if m:
            val = m.group(1).strip().strip(QUOTE_CHARS)
            if val and 1 <= len(val) <= 128:
                cleaned = val

        # 2. 若未匹配到内嵌密码，尝试剥离标准行首前缀
        if not cleaned:
            val = PREFIX_PATTERN.sub("", line).strip().strip(QUOTE_CHARS)
            if val and 1 <= len(val) <= 128:
                cleaned = val

        # 3. 约束合理长度（1 ~ 128 字符）并去重
        if cleaned and 1 <= len(cleaned) <= 128:
            if cleaned not in seen:
                seen.add(cleaned)
                candidates.append(cleaned)
    return candidates


def read_text_safe(file_path: Path | str) -> str:
    """自动适配 UTF-8 (含 BOM)、GB18030 (ANSI/GBK)、Big5 编码。"""
    p = Path(file_path)
    raw = p.read_bytes()

    # 1. 优先尝试 UTF-8（含 BOM）
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            pass

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass

    # 2. 针对非 UTF-8，智能甄别 GB18030 与 Big5
    candidates = {}
    for enc in ("big5", "gb18030"):
        try:
            candidates[enc] = raw.decode(enc)
        except UnicodeDecodeError:
            continue

    if not candidates:
        return raw.decode("utf-8", errors="replace")

    if len(candidates) == 1:
        return next(iter(candidates.values()))

    # 若两者均能解码，通过密码领域常用词与特征打分确定最佳编码
    keywords = (
        "密码", "密碼", "解压", "解壓", "提取码", "提取碼",
        "提取", "链接", "鏈接", "解压码", "解壓碼", "繁體", "简体"
    )
    scores = {}
    for enc, text in candidates.items():
        score = sum(text.count(kw) * 10 for kw in keywords)
        cjk_count = sum(1 for ch in text if 0x4E00 <= ord(ch) <= 0x9FFF or 0x20 <= ord(ch) <= 0x7E)
        scores[enc] = score + cjk_count

    best_enc = max(scores, key=lambda k: (scores[k], 1 if k == "gb18030" else 0))
    return candidates[best_enc]


class PasswordManager:
    """
    密码库单例管理器，提供线程安全 CRUD、使用频率自学习排序、
    批量导入与原子 JSON 持久化。
    """

    _instance = None
    _lock = threading.RLock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self):
        with self._lock:
            if getattr(self, "_initialized", False):
                return
            if getattr(sys, "frozen", False):
                self._file = Path(sys.executable).parent / "data" / "passwords.json"
            else:
                self._file = Path(__file__).resolve().parent / "passwords.json"
            self._cache: list[dict[str, Any]] = []
            self._loaded = False
            self._initialized = True

    # ---------- 内部方法 ----------

    @staticmethod
    def _normalize_item(raw_item: Any) -> dict[str, Any]:
        """向前兼容归一化：将纯字符串或缺失字段的旧字典统一转换为标准结构。"""
        if isinstance(raw_item, str):
            return {
                "password": raw_item,
                "tag": "[历史导入]",
                "count": 0,
                "last_used": 0.0,
            }
        if isinstance(raw_item, dict):
            pwd = str(raw_item.get("password", ""))
            tag = str(raw_item.get("tag", ""))
            try:
                count = int(raw_item.get("count", 0))
            except (ValueError, TypeError):
                count = 0
            try:
                last_used = float(raw_item.get("last_used", 0.0))
            except (ValueError, TypeError):
                last_used = 0.0
            return {
                "password": pwd,
                "tag": tag,
                "count": count,
                "last_used": last_used,
            }
        return {
            "password": str(raw_item),
            "tag": "",
            "count": 0,
            "last_used": 0.0,
        }

    def _sort_cache(self):
        """按命中次数 count 降序，再按最后使用时间 last_used 降序。"""
        def _get_sort_key(x: dict[str, Any]):
            c = x.get("count", 0)
            try:
                c_val = int(c) if c is not None else 0
            except (ValueError, TypeError):
                c_val = 0
            lu = x.get("last_used", 0.0)
            try:
                lu_val = float(lu) if lu is not None else 0.0
            except (ValueError, TypeError):
                lu_val = 0.0
            return (c_val, lu_val)

        self._cache.sort(key=_get_sort_key, reverse=True)

    def _ensure_loaded(self):
        """懒加载：首次访问时从 JSON 文件读取。"""
        if self._loaded:
            return
        self.reload()

    def reload(self):
        """强制从磁盘重新加载密码库并按频率降序重排。"""
        with self._lock:
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
                self._cache = [
                    self._normalize_item(item)
                    for item in raw
                    if item
                ]
                # 剔除空密码
                self._cache = [item for item in self._cache if item.get("password") and str(item.get("password")).strip()]
                self._sort_cache()
                self._loaded = True
                logger.info(f"密码库已加载，共 {len(self._cache)} 条记录")
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning(f"密码库文件损坏 ({e})，降级为空列表")
                self._cache = []
                self._loaded = True

    def _persist(self):
        """将当前缓存排序后写回 JSON 文件（铁律三：ensure_ascii=False + utf-8）。"""
        self._sort_cache()
        self._file.parent.mkdir(parents=True, exist_ok=True)
        with open(self._file, "w", encoding="utf-8") as f:
            json.dump(self._cache, f, ensure_ascii=False, indent=2)
        logger.debug(f"密码库已持久化 ({len(self._cache)} 条)")

    # ---------- 公开 CRUD 接口 ----------

    def get_all(self) -> list[dict[str, Any]]:
        """获取全部密码记录（返回副本，已按频率降序排列）。"""
        with self._lock:
            self._ensure_loaded()
            return [dict(item) for item in self._cache]

    def add(
        self,
        password: str,
        tag: str,
        count: int = 0,
        last_used: float = 0.0,
    ) -> int:
        """
        添加一条密码记录（若已存在则更新标签与信息并重排）。
        返回该记录在重排后的索引；密码为空时返回 -1。
        """
        if not password or not str(password).strip():
            logger.warning("尝试添加空密码，已忽略")
            return -1
        password = str(password).strip()

        with self._lock:
            self._ensure_loaded()
            existing = None
            for item in self._cache:
                if item["password"] == password:
                    existing = item
                    break
            if existing is not None:
                if tag:
                    existing["tag"] = tag
                if count > 0:
                    existing["count"] = max(existing.get("count", 0), count)
                if last_used > 0:
                    existing["last_used"] = max(existing.get("last_used", 0.0), last_used)
            else:
                record = {
                    "password": password,
                    "tag": tag,
                    "count": count,
                    "last_used": last_used,
                }
                self._cache.append(record)
            self._persist()
            # 找到重排后的索引
            idx = 0
            for i, item in enumerate(self._cache):
                if item["password"] == password:
                    idx = i
                    break
            logger.info(f"密码已添加/更新 [索引={idx}]，标签: {tag}")
            return idx

    def record_hit(self, password: str):
        """
        解压成功时自学习打点：对应密码使用次数+1，更新时间戳并触发重排与写盘。
        若密码不在库中，则自动追加并设 count=1。若传入空密码则忽略。
        """
        if not password or not str(password).strip():
            return
        password = str(password).strip()

        with self._lock:
            self._ensure_loaded()
            now = time.time()
            found = False
            for item in self._cache:
                if item["password"] == password:
                    item["count"] = item.get("count", 0) + 1
                    item["last_used"] = now
                    found = True
                    break
            if not found:
                self._cache.append({
                    "password": password,
                    "tag": "[自动入库]",
                    "count": 1,
                    "last_used": now,
                })
            self._persist()
            logger.info(f"密码频次已自学习自增: '{password}'")

    def batch_add(
        self,
        passwords: list[str],
        tag: str = "[批量导入]",
    ) -> tuple[int, int]:
        """
        批量添加密码，自动去重。
        返回: (成功新增数, 忽略重复数)
        """
        with self._lock:
            self._ensure_loaded()
            existing_pwds = {item["password"] for item in self._cache}
            added = 0
            duplicates = 0
            for raw_pwd in passwords:
                pwd = raw_pwd.strip()
                if not pwd:
                    continue
                if pwd in existing_pwds:
                    duplicates += 1
                else:
                    existing_pwds.add(pwd)
                    self._cache.append({
                        "password": pwd,
                        "tag": tag,
                        "count": 0,
                        "last_used": 0.0,
                    })
                    added += 1

            if added > 0:
                self._persist()
            logger.info(f"批量导入完成: 新增 {added} 条, 重复 {duplicates} 条")
            return added, duplicates

    def delete_by_password(self, password: str) -> bool:
        """
        按密码内容精确删除记录（解决排序导致行索引错位问题）。
        返回是否成功删除。
        """
        with self._lock:
            self._ensure_loaded()
            target_idx = None
            for i, item in enumerate(self._cache):
                if item["password"] == password:
                    target_idx = i
                    break
            if target_idx is None:
                logger.warning(f"删除失败：未找到密码记录 '{password}'")
                return False
            removed = self._cache.pop(target_idx)
            self._persist()
            logger.info(f"密码已精确删除: '{password}', 标签: {removed.get('tag')}")
            return True

    def delete(self, index: int) -> dict[str, Any] | None:
        """
        按索引删除密码记录（向下兼容）。
        返回被删除的记录；索引无效时返回 None。
        """
        with self._lock:
            self._ensure_loaded()
            if index < 0 or index >= len(self._cache):
                logger.warning(f"删除失败：索引 {index} 越界（总数 {len(self._cache)}）")
                return None
            removed = self._cache.pop(index)
            self._persist()
            logger.info(f"密码已删除 [索引={index}]，标签: {removed.get('tag')}")
            return removed

    def clear_all(self) -> int:
        """清空全部密码记录。返回被删除的记录总数。"""
        with self._lock:
            self._ensure_loaded()
            count = len(self._cache)
            self._cache.clear()
            self._persist()
            logger.info(f"密码库已清空，共删除 {count} 条记录")
            return count

    @property
    def count(self) -> int:
        """当前密码库记录总数。"""
        with self._lock:
            self._ensure_loaded()
            return len(self._cache)