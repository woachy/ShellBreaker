"""
密码数据层 — 密码库的 JSON 持久化读写、使用频率自学习与批量导入解析。
严格遵循【铁律三】：ensure_ascii=False + utf-8 编码，完美兼容纯中文密码。
"""
import json
import logging
import os
import re
import sys
import tempfile
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


_ENCODING_KEYWORDS = (
    "密码", "密碼", "解压", "解壓", "提取码", "提取碼",
    "提取", "链接", "鏈接", "解压码", "解壓碼", "繁體", "简体",
    "说明", "說明", "下载", "下載", "文件", "檔案", "压缩", "壓縮",
)

# 上下文嗅探常量与正则
_CONTEXT_MAX_TXT_SIZE = 512 * 1024
_CONTEXT_MAX_TXT_FILES = 20
_CONTEXT_TXT_EXTS = frozenset({".txt", ".nfo"})
_CONTEXT_PRIMARY_FILENAME_HINTS = ("密码", "密碼", "解压", "解壓", "password", "passwd", "pwd")
_CONTEXT_SECONDARY_FILENAME_HINTS = ("说明", "說明", "readme", "read_me", "提取码", "提取碼", "必看", "注意")

_MULTI_ARCHIVE_SUFFIX_RE = re.compile(
    r"(?:\.part\d+\.(?:rar|zip|7z|exe)|\.(?:7z|zip|rar|tar)\.\d{2,}|\.tar\.(?:gz|bz2|xz|zst|lz4))$",
    re.IGNORECASE,
)

_CONTEXT_KEYWORD_CORE = (
    r"解压密码|解壓密碼|解压码|解壓碼|提取码|提取碼"
    r"|(?<![无無免])(?<!没有)(?<!沒有)(?<!无需)(?<!無需)(?:密码|密碼)"
    r"|(?<![A-Za-z])(?:password|passwd|pwd|pass|pw)"
)

_CONTEXT_KEYWORD_RE = re.compile(_CONTEXT_KEYWORD_CORE, re.IGNORECASE)

_CONTEXT_EXPLICIT_RE = re.compile(
    rf"(?P<kw>{_CONTEXT_KEYWORD_CORE})"
    r"(?:\s*[】\]]\s*[:：=\-\s]*|\s*[:：=\-\s]\s*)"
    r"(?!https?://|ftp://|magnet:|ed2k://|thunder://)"
    r"[\"'“”‘’【\[《<（(]?"
    r"(?P<val>[^\s/\\|,:;，；。！？()\[\]【】（）\"'“”‘’<>《》]{1,64})",
    re.IGNORECASE,
)

_CONTEXT_EMPTY_AFTER_KW_RE = re.compile(
    rf"(?:{_CONTEXT_KEYWORD_CORE})\s*[】\]]?\s*[:：=\-]?\s*$",
    re.IGNORECASE,
)

_CONTEXT_BRACKET_RE = re.compile(r"【([^【】]{2,32})】|\[([^\[\]]{2,32})\]")

_URL_PATTERN_RE = re.compile(
    r"(?:https?://|ftp://|magnet:|ed2k://|thunder://|://|www\.[^\s/]+/)",
    re.IGNORECASE,
)

_CONTEXT_TRAILING_PUNCT = QUOTE_CHARS + ",;:，。；：、！？."

_CONTEXT_PLACEHOLDERS = frozenset({
    "无", "無", "没有", "沒有", "无密码", "無密碼", "无需密码", "無需密碼",
    "none", "null", "nil", "n/a", "na", "默认", "见说明", "看说明", "同上", "不详",
})

_BRACKET_EXCLUDE_TAGS = frozenset({
    # 常见中文资源标签
    "合集", "合辑", "高清", "超清", "蓝光", "完整版", "精简版", "珍藏版", "典藏版",
    "豪华版", "终极版", "特别版", "正式版", "测试版", "破解版", "免安装", "免安装版",
    "绿色版", "硬盘版", "学习版", "无损", "汉化", "汉化版", "官中", "官方", "官方中文",
    "中文", "中文版", "简中", "繁中", "中文字幕", "双语", "国语", "粤语", "日语", "英语",
    "全集", "完结", "资源", "游戏", "电影", "动漫", "漫画", "音乐", "软件", "教程",
    "图包", "套图", "写真", "合集版", "最新", "最新版", "重置版", "重制版", "修复版",
    "补丁", "整合版", "去码", "无码", "步兵", "骑兵", "无修", "生肉", "熟肉", "机翻",
    "精翻", "原创", "转载", "搬运", "首发", "独家", "免费", "网盘", "百度网盘", "夸克",
    "迅雷", "解压", "密码", "无密码", "无需密码",
    # 常见英文/视频/压缩格式标签
    "1080p", "720p", "480p", "2160p", "4k", "8k", "2k", "hd", "fhd", "uhd",
    "bd", "bluray", "bdrip", "webrip", "web-dl", "hdr", "hdr10",
    "x264", "x265", "h264", "h265", "hevc", "avc",
    "flac", "mp3", "aac", "wav", "ape",
    "mp4", "mkv", "avi", "mov", "wmv", "flv", "webm",
    "rar", "zip", "7z", "tar", "gz", "bz2", "xz", "iso", "apk", "exe",
    "pdf", "epub", "txt", "jpg", "jpeg", "png", "gif", "raw",
    "pc", "mac", "win", "windows", "linux", "android", "ios", "switch", "ns", "ps4", "ps5",
    "dlc", "ost", "ova", "oad", "sp", "tv",
    "chs", "cht", "cn", "jp", "en", "eng", "sub", "dub",
    "final", "full", "repack", "rip", "goty", "remastered", "uncensored", "censored",
    "new", "hot", "vip",
})

_BRACKET_EXCLUDE_PATTERNS = (
    re.compile(r"^part\s*\d+$", re.IGNORECASE),
    re.compile(r"^vol\.?\s*\d+$", re.IGNORECASE),
    re.compile(r"^ep\.?\s*\d+$", re.IGNORECASE),
    re.compile(r"^v\d+(?:\.\d+)*$", re.IGNORECASE),
    re.compile(r"^\d+(?:\.\d+)?\s*(?:kb|mb|gb|tb|g|m)$", re.IGNORECASE),
)


def _score_decoded_text(text: str) -> float:
    """对候选编码的解码文本进行可读性打分，并对私用区/替换符/控制符/非常用扩展施加惩罚。"""
    score = sum(text.count(kw) * 15.0 for kw in _ENCODING_KEYWORDS)

    normal_count = 0
    pua_count = 0
    replacement_count = 0
    ctrl_count = 0
    uncommon_count = 0
    non_ws_count = 0

    for ch in text:
        if not ch.isspace():
            non_ws_count += 1
        code = ord(ch)
        if code == 0xFFFD:
            replacement_count += 1
        elif 0xE000 <= code <= 0xF8FF or 0xF0000 <= code <= 0x10FFFF:
            pua_count += 1
        elif (code < 0x20 and ch not in ("\t", "\n", "\r")) or (0x7F <= code <= 0x9F):
            ctrl_count += 1
        elif (
            0x3400 <= code <= 0x4DBF
            or 0xF900 <= code <= 0xFAFF
            or 0xFE10 <= code <= 0xFE6F
            or 0x3100 <= code <= 0x312F
            or code >= 0x20000
        ):
            uncommon_count += 1
        elif (
            0x4E00 <= code <= 0x9FFF
            or 0x20 <= code <= 0x7E
            or 0x3000 <= code <= 0x303F
            or 0xFF01 <= code <= 0xFF5E
        ):
            normal_count += 1

    penalty = (
        pua_count * 20.0
        + replacement_count * 30.0
        + ctrl_count * 20.0
        + uncommon_count * 8.0
    )
    bad_count = pua_count + replacement_count + ctrl_count + uncommon_count
    if non_ws_count > 0 and bad_count > 0:
        penalty += (bad_count / non_ws_count) * 100.0

    return score + normal_count - penalty


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
    candidates: dict[str, str] = {}
    for enc in ("gb18030", "big5"):
        try:
            candidates[enc] = raw.decode(enc)
        except UnicodeDecodeError:
            continue

    if not candidates:
        return raw.decode("utf-8", errors="replace")

    if len(candidates) == 1:
        return next(iter(candidates.values()))

    # 3. 两者均能无报错解码：结合领域关键词、可读字符与私用区/乱码惩罚综合打分
    #    同分时默认优先选用 gb18030（简体中文 Windows 密码本主流编码）
    gb_score = round(_score_decoded_text(candidates["gb18030"]), 6)
    big5_score = round(_score_decoded_text(candidates["big5"]), 6)
    if gb_score >= big5_score:
        return candidates["gb18030"]
    return candidates["big5"]


def _strip_archive_stem(filename: str) -> str:
    """剥离压缩包多重后缀（如 .part1.rar / .7z.001 / .tar.gz）或常规扩展名。"""
    if not filename:
        return ""
    stripped = _MULTI_ARCHIVE_SUFFIX_RE.sub("", filename)
    if stripped != filename:
        return stripped
    return Path(filename).stem


def _clean_contextual_candidate(raw_val: str, max_len: int = 64) -> str:
    """清洗上下文嗅探到的单个密码候选值，过滤网址、空值、占位词与超长文本。"""
    if not raw_val:
        return ""
    val = raw_val.strip().strip(QUOTE_CHARS).rstrip(_CONTEXT_TRAILING_PUNCT).strip(QUOTE_CHARS)
    if not val or len(val) > max_len:
        return ""
    if _URL_PATTERN_RE.search(val):
        return ""
    if "/" in val or "\\" in val:
        return ""
    if val.lower() in _CONTEXT_PLACEHOLDERS:
        return ""
    return val


def _extract_explicit_from_text(text: str) -> list[str]:
    """从单段文本中提取显式密码标注，非提取码类（如解压密码/password）排在提取码之前。"""
    if not text:
        return []
    primary: list[str] = []
    secondary: list[str] = []
    seen: set[str] = set()

    for m in _CONTEXT_EXPLICIT_RE.finditer(text):
        raw_val = m.group("val")
        # 拦截类似 www.example.com/path 在 / 处被截断的情况
        if (
            raw_val.lower().startswith("www.")
            and m.end() < len(text)
            and text[m.end()] == "/"
        ):
            continue
        cleaned = _clean_contextual_candidate(raw_val)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        kw = m.group("kw").lower()
        if kw in ("提取码", "提取碼"):
            secondary.append(cleaned)
        else:
            primary.append(cleaned)

    return primary + secondary


def _extract_from_readme_file(file_path: Path) -> list[str]:
    """从单个同目录 .txt/.nfo 文件中提取包含密码关键词的同行或紧邻下一短行候选密码。"""
    results: list[str] = []
    seen: set[str] = set()

    def _add(val: str) -> None:
        cleaned = _clean_contextual_candidate(val)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            results.append(cleaned)

    try:
        content = read_text_safe(file_path)
    except Exception as e:
        logger.debug(f"上下文嗅探读取说明文件失败 ({file_path}): {e}")
        return results

    lines = content.splitlines()
    total_lines = len(lines)
    for idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or line.startswith(("#", "//", ";")):
            continue
        if not _CONTEXT_KEYWORD_RE.search(line):
            continue

        line_hits = _extract_explicit_from_text(line)
        if line_hits:
            for h in line_hits:
                _add(h)
            continue

        # 协同 parse_passwords_text 处理前缀行（如 '【解压码】：secret'）
        if PREFIX_PATTERN.match(line):
            before_count = len(results)
            for parsed in parse_passwords_text(line):
                if len(parsed) <= 64 and not any(ch.isspace() for ch in parsed):
                    _add(parsed)
            if len(results) > before_count:
                continue

        # 若该行冒号/关键词后为空，探测紧邻下一非空短行作为候选
        if _CONTEXT_EMPTY_AFTER_KW_RE.search(line):
            for look_ahead in range(idx + 1, min(idx + 3, total_lines)):
                next_raw = lines[look_ahead].strip()
                if not next_raw or next_raw.startswith(("#", "//", ";")):
                    continue
                if (
                    len(next_raw) <= 64
                    and not _CONTEXT_EMPTY_AFTER_KW_RE.search(next_raw)
                    and not _URL_PATTERN_RE.search(next_raw)
                    and not any(p in next_raw for p in ("。", "！", "？", "，", "；"))
                ):
                    parsed_next = parse_passwords_text(next_raw)
                    if parsed_next:
                        cand = parsed_next[0]
                        if not any(ch.isspace() for ch in cand):
                            _add(cand)
                break

    return results


def _is_excluded_bracket_tag(token: str) -> bool:
    """判断括号内短字段是否为常见非密码资源标签。"""
    low = token.lower()
    if low in _BRACKET_EXCLUDE_TAGS or low in _CONTEXT_PLACEHOLDERS:
        return True
    if _CONTEXT_KEYWORD_RE.search(token):
        return True
    if _URL_PATTERN_RE.search(token):
        return True
    if any(pat.match(token) for pat in _BRACKET_EXCLUDE_PATTERNS):
        return True
    if any(ch.isspace() for ch in token):
        return True
    return False


def _extract_bracket_candidates(stem: str) -> list[str]:
    """提取文件名中 【...】 或 [...] 内的短字段作为兜底上下文候选（长度 2..32）。"""
    if not stem:
        return []
    candidates: list[str] = []
    seen: set[str] = set()
    for m in _CONTEXT_BRACKET_RE.finditer(stem):
        raw_token = (m.group(1) or m.group(2) or "").strip()
        cleaned = _clean_contextual_candidate(raw_token, max_len=32)
        if not cleaned or not (2 <= len(cleaned) <= 32):
            continue
        if _is_excluded_bracket_tag(cleaned):
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            candidates.append(cleaned)
    return candidates


def extract_contextual_passwords(archive_path: str | Path) -> list[str]:
    """
    从压缩包上下文自动嗅探候选密码（按优先级去重返回，异常安全不抛错）：
    1. 压缩包文件名显式密码标注（最高优先级）
    2. 父目录名显式密码标注
    3. 同目录说明文本文件（.txt / .nfo，体积 < 512KB，最多扫描前 20 个优先排序文件）
    4. 文件名中 【...】 / [...] 内的短字段兜底候选（长度 2..32，过滤常见资源标签）
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def _append(values: list[str]) -> None:
        for val in values:
            if val and val not in seen:
                seen.add(val)
                candidates.append(val)

    try:
        p = Path(archive_path)
    except Exception:
        return candidates

    # 1. 压缩包文件名显式密码标注
    stem = ""
    try:
        stem = _strip_archive_stem(p.name)
        _append(_extract_explicit_from_text(stem))
    except Exception as e:
        logger.debug(f"上下文嗅探文件名解析异常: {e}")

    # 2. 父目录名显式密码标注
    try:
        parent_name = p.parent.name
        if parent_name:
            _append(_extract_explicit_from_text(parent_name))
    except Exception as e:
        logger.debug(f"上下文嗅探父目录名解析异常: {e}")

    # 3. 同目录说明文本文件（.txt / .nfo，0 < size < 512KB，最多前 20 个）
    try:
        parent_dir = p.parent
        if parent_dir and parent_dir.is_dir():
            txt_entries: list[tuple[int, str, Path]] = []
            try:
                with os.scandir(parent_dir) as it:
                    for entry in it:
                        try:
                            if not entry.is_file(follow_symlinks=True):
                                continue
                            name_lower = entry.name.lower()
                            _, ext = os.path.splitext(name_lower)
                            if ext not in _CONTEXT_TXT_EXTS:
                                continue
                            size = entry.stat(follow_symlinks=True).st_size
                            if not (0 < size < _CONTEXT_MAX_TXT_SIZE):
                                continue
                            if any(h in name_lower for h in _CONTEXT_PRIMARY_FILENAME_HINTS):
                                prio = 0
                            elif any(h in name_lower for h in _CONTEXT_SECONDARY_FILENAME_HINTS):
                                prio = 1
                            else:
                                prio = 2
                            txt_entries.append((prio, name_lower, Path(entry.path)))
                        except OSError:
                            continue
            except OSError as e:
                logger.debug(f"上下文嗅探扫描目录失败 ({parent_dir}): {e}")

            txt_entries.sort(key=lambda item: (item[0], item[1]))
            for _, _, txt_path in txt_entries[:_CONTEXT_MAX_TXT_FILES]:
                _append(_extract_from_readme_file(txt_path))
    except Exception as e:
        logger.debug(f"上下文嗅探同目录说明文件异常: {e}")

    # 4. 文件名末尾/内嵌括号候选（兜底上下文）
    try:
        if stem:
            _append(_extract_bracket_candidates(stem))
    except Exception as e:
        logger.debug(f"上下文嗅探括号兜底解析异常: {e}")

    return candidates


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
        """将当前缓存排序后原子写回 JSON 文件（铁律三：ensure_ascii=False + utf-8）。"""
        with self._lock:
            self._sort_cache()
            self._file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self._file.parent,
                    prefix=f"{self._file.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as f:
                    tmp_path = Path(f.name)
                    json.dump(self._cache, f, ensure_ascii=False, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, self._file)
            except Exception:
                if tmp_path is not None and tmp_path.exists():
                    try:
                        tmp_path.unlink()
                    except OSError:
                        pass
                raise
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

    def _record_usage(self, password: str, tag: str = "", default_tag: str = "[自动入库]") -> None:
        """
        内部自学习打点实现：
        - 若密码已在库中：count += 1、更新 last_used 并触发重排与写盘。
        - 若密码尚不在库中：自动以 tag or default_tag、count=1、last_used=now 插入并持久化。
        - 若传入空密码则忽略。
        """
        if not password or not str(password).strip():
            return
        pwd = str(password).strip()

        with self._lock:
            self._ensure_loaded()
            now = time.time()
            found = False
            for item in self._cache:
                if item["password"] == pwd:
                    try:
                        cur_count = int(item.get("count", 0) or 0)
                    except (ValueError, TypeError):
                        cur_count = 0
                    item["count"] = cur_count + 1
                    item["last_used"] = now
                    found = True
                    break
            if not found:
                self._cache.append({
                    "password": pwd,
                    "tag": tag or default_tag,
                    "count": 1,
                    "last_used": now,
                })
            self._persist()
            logger.info(f"密码频次已自学习自增: '{pwd}'")

    def record_hit(self, password: str, tag: str = ""):
        """
        解压成功时自学习打点：对应密码使用次数+1，更新时间戳并触发重排与写盘。
        若密码不在库中，则自动追加并设 count=1。若传入空密码则忽略。
        """
        self._record_usage(password, tag=tag, default_tag="[自动入库]")

    def record_success(self, password: str, tag: str = ""):
        """
        解压成功时自学习打点（支持上下文嗅探新密码自动入库）：
        - 若密码已在库中：count += 1、last_used = time.time() 并按频次重排持久化。
        - 若密码尚不在库中：自动以 tag or '自动嗅探'、count=1、last_used=time.time() 插入并持久化。
        """
        self._record_usage(password, tag=tag, default_tag="自动嗅探")

    def extract_contextual_passwords(self, archive_path: str | Path) -> list[str]:
        """从压缩包文件名、父目录名、同目录说明文本及括号标签自动嗅探上下文候选密码。"""
        return extract_contextual_passwords(archive_path)

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