"""
系统集成服务模块 (单实例 IPC / 监听文件夹)
提供与操作系统集成的核心能力：
1. 基于 QLocalServer / QLocalSocket 的单实例本地 IPC 参数转发桥接；
2. 基于 QFileSystemWatcher + 定时稳定性检测的监听文件夹自动破壳服务。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from core.sandbox import (
    KNOWN_ARCHIVE_EXTS,
    POTENTIAL_DISGUISE_EXTS,
    SANDBOX_CONTAINER_NAME,
    is_secondary_volume,
)

logger = logging.getLogger("ShellBreaker.SystemIntegration")

# --------------- 单实例 IPC 常量 ---------------
DEFAULT_IPC_SERVER_NAME = "ShellBreaker_SingleInstance_IPC_v1"

# --------------- 监听文件夹常量 ---------------
IGNORED_TEMP_EXTS = frozenset({
    ".crdownload",
    ".part",
    ".tmp",
    ".downloading",
    ".!ut",
    ".aria2",
    ".bc!",
    ".opdownload",
    ".partial",
})

_WATCH_CANDIDATE_EXTS = frozenset(KNOWN_ARCHIVE_EXTS) | frozenset(POTENTIAL_DISGUISE_EXTS)


# =====================================================================
# 1. 单实例 IPC 桥接（QLocalServer / QLocalSocket）
# =====================================================================

class SingleInstanceBridge(QObject):
    """基于本地命名管道/套接字的单实例检测与命令行路径转发桥接器。"""

    paths_received = Signal(list)

    def __init__(
        self,
        parent: Optional[QObject] = None,
        server_name: str = DEFAULT_IPC_SERVER_NAME,
    ) -> None:
        super().__init__(parent)
        self._server_name = server_name
        self._server: Optional[QLocalServer] = None
        self._client_buffers: dict[QLocalSocket, bytearray] = {}

    def try_forward_to_existing(
        self,
        paths: list[str],
        timeout_ms: int = 800,
    ) -> bool:
        """
        尝试连接已运行实例的本地 IPC 服务并转发路径列表。
        若连接成功并收到服务端应答 b"OK\\n"，返回 True；否则返回 False。
        """
        socket = QLocalSocket()
        try:
            socket.connectToServer(self._server_name)
            if not socket.waitForConnected(timeout_ms):
                socket.abort()
                return False

            payload = (
                json.dumps({"paths": [str(p) for p in paths]}, ensure_ascii=False)
                + "\n"
            ).encode("utf-8")
            socket.write(payload)
            socket.flush()
            if socket.bytesToWrite() > 0 and not socket.waitForBytesWritten(timeout_ms):
                socket.abort()
                return False

            deadline = time.monotonic() + (timeout_ms / 1000.0)
            ack = bytearray(bytes(socket.readAll()))
            while b"\n" not in ack:
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    break
                if not socket.waitForReadyRead(remaining_ms):
                    break
                ack.extend(bytes(socket.readAll()))

            socket.disconnectFromServer()
            if socket.state() != QLocalSocket.LocalSocketState.UnconnectedState:
                socket.waitForDisconnected(min(timeout_ms, 200))
            return b"OK" in bytes(ack)
        finally:
            socket.close()

    def start_server(self) -> bool:
        """启动本地 IPC 服务端监听，清理可能遗留的陈旧命名管道。"""
        self.stop_server()
        QLocalServer.removeServer(self._server_name)

        server = QLocalServer(self)
        server.newConnection.connect(self._on_new_connection)
        if not server.listen(self._server_name):
            logger.error(
                f"SingleInstanceBridge 启动监听失败 ({self._server_name}): "
                f"{server.errorString()}"
            )
            server.deleteLater()
            return False

        self._server = server
        logger.info(f"SingleInstanceBridge 已启动监听: {self._server_name}")
        return True

    def stop_server(self) -> None:
        """安全关闭本地 IPC 服务端及所有活跃客户端连接。"""
        for client in list(self._client_buffers.keys()):
            try:
                client.close()
                client.deleteLater()
            except Exception:
                pass
        self._client_buffers.clear()

        if self._server is not None:
            try:
                self._server.close()
                self._server.deleteLater()
            except Exception:
                pass
            self._server = None

    def _on_new_connection(self) -> None:
        if self._server is None:
            return
        while self._server.hasPendingConnections():
            client = self._server.nextPendingConnection()
            if client is None:
                continue
            self._client_buffers[client] = bytearray()
            client.readyRead.connect(
                lambda c=client: self._on_client_ready_read(c)
            )
            client.disconnected.connect(
                lambda c=client: self._cleanup_client(c)
            )
            if client.bytesAvailable() > 0:
                self._on_client_ready_read(client)

    def _on_client_ready_read(self, client: QLocalSocket) -> None:
        buf = self._client_buffers.get(client)
        if buf is None:
            return
        buf.extend(bytes(client.readAll()))
        if b"\n" not in buf:
            return

        line, _, rest = buf.partition(b"\n")
        self._client_buffers[client] = bytearray(rest)
        paths = self._parse_paths_payload(line)

        try:
            client.write(b"OK\n")
            client.flush()
        except Exception as e:
            logger.debug(f"IPC 写回确认失败: {e}")

        self.paths_received.emit(paths)

    def _cleanup_client(self, client: QLocalSocket) -> None:
        buf = self._client_buffers.pop(client, None)
        if buf:
            remaining = bytes(buf).strip()
            if remaining:
                paths = self._parse_paths_payload(remaining)
                self.paths_received.emit(paths)
        try:
            client.deleteLater()
        except Exception:
            pass

    @staticmethod
    def _parse_paths_payload(raw_line: bytes) -> list[str]:
        try:
            text = raw_line.decode("utf-8").strip()
            if not text:
                return []
            data = json.loads(text)
            if isinstance(data, dict):
                raw_paths = data.get("paths", [])
                if isinstance(raw_paths, list):
                    return [str(p) for p in raw_paths]
        except Exception as e:
            logger.warning(f"IPC 载荷解析失败: {e}")
        return []


# =====================================================================
# 2. 监听文件夹自动破壳服务（WatchFolderService）
# =====================================================================

class WatchFolderService(QObject):
    """
    监听指定目录新增压缩包/伪装包文件，待其写入体积稳定且解除独占锁后发射 file_ready 信号。
    """

    file_ready = Signal(str)

    def __init__(
        self,
        parent: Optional[QObject] = None,
        stable_seconds: float = 3.0,
        poll_interval_ms: int = 1000,
    ) -> None:
        super().__init__(parent)
        self._stable_seconds = max(0.0, float(stable_seconds))
        self._poll_interval_ms = max(20, int(poll_interval_ms))
        self._watch_dir: Optional[Path] = None
        self._running = False

        self._known_files: set[Path] = set()
        # path -> (size, mtime_ns, first_stable_monotonic)
        self._pending: dict[Path, tuple[int, int, float]] = {}

        self._watcher: Optional[QFileSystemWatcher] = None

        self._timer = QTimer(self)
        self._timer.setInterval(self._poll_interval_ms)
        self._timer.timeout.connect(self._scan_once)

    def start(self, folder_path: str | Path) -> bool:
        """开始监听指定目录，预先记录已有旧文件避免误触发。"""
        try:
            target = Path(folder_path).resolve()
        except OSError:
            return False

        if not target.exists() or not target.is_dir():
            return False

        self.stop()

        try:
            for entry in target.iterdir():
                if entry.is_file():
                    self._known_files.add(entry.resolve())
        except OSError as e:
            logger.warning(f"初始化监听目录快照失败 ({target}): {e}")
            return False

        self._watch_dir = target
        self._running = True
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_directory_changed)
        self._watcher.addPath(str(target))
        self._timer.start(self._poll_interval_ms)
        logger.info(
            f"WatchFolderService 已启动: {target} "
            f"(已有文件={len(self._known_files)}, 稳定阈值={self._stable_seconds}s)"
        )
        return True

    def stop(self) -> None:
        """停止监听并清理待检测状态。"""
        self._running = False
        self._timer.stop()
        if self._watcher is not None:
            watched_dirs = self._watcher.directories()
            if watched_dirs:
                self._watcher.removePaths(watched_dirs)
            try:
                self._watcher.directoryChanged.disconnect(self._on_directory_changed)
            except (RuntimeError, TypeError):
                pass
            self._watcher.deleteLater()
            self._watcher = None
        self._watch_dir = None
        self._known_files.clear()
        self._pending.clear()

    def is_running(self) -> bool:
        """返回服务是否处于运行状态。"""
        return self._running

    def watch_dir(self) -> Optional[Path]:
        """返回当前正在监听的目录路径，未运行时返回 None。"""
        return self._watch_dir

    def _on_directory_changed(self, _path: str) -> None:
        if self._running:
            self._scan_once()

    @staticmethod
    def _is_candidate_file(path: Path) -> bool:
        """判断直接子文件是否为需关注的新增压缩包/伪装包候选文件。"""
        name = path.name
        if name == SANDBOX_CONTAINER_NAME or name.startswith("."):
            return False
        ext = path.suffix.lower()
        if ext in IGNORED_TEMP_EXTS:
            return False
        if ext not in _WATCH_CANDIDATE_EXTS:
            return False
        if is_secondary_volume(name):
            return False
        return True

    def _scan_once(self) -> None:
        if not self._running or self._watch_dir is None:
            return

        watch_root = self._watch_dir
        if not watch_root.is_dir():
            return

        now = time.monotonic()
        seen_candidates: set[Path] = set()

        try:
            entries = list(watch_root.iterdir())
        except OSError as e:
            logger.debug(f"扫描监听目录失败 ({watch_root}): {e}")
            return

        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                resolved = entry.resolve()
            except OSError:
                continue

            if resolved in self._known_files:
                continue
            if not self._is_candidate_file(resolved):
                continue

            seen_candidates.add(resolved)

            try:
                st = resolved.stat()
            except OSError:
                continue

            size = st.st_size
            mtime_ns = st.st_mtime_ns

            prev = self._pending.get(resolved)
            if prev is None:
                self._pending[resolved] = (size, mtime_ns, now)
                continue

            prev_size, prev_mtime_ns, first_stable = prev
            if size <= 0 or size != prev_size or mtime_ns != prev_mtime_ns:
                self._pending[resolved] = (size, mtime_ns, now)
                continue

            if (now - first_stable) < self._stable_seconds:
                continue

            # 尝试以只读二进制方式打开，确认未被浏览器等进程独占锁住
            try:
                with open(resolved, "rb"):
                    pass
            except OSError:
                self._pending[resolved] = (size, mtime_ns, now)
                continue

            self._known_files.add(resolved)
            self._pending.pop(resolved, None)
            logger.info(f"WatchFolderService 检测到稳定新文件: {resolved}")
            self.file_ready.emit(str(resolved))

        # 清理稳定前已被删除或重命名消失的待检测记录
        for stale_path in list(self._pending.keys()):
            if stale_path not in seen_candidates:
                self._pending.pop(stale_path, None)


__all__ = [
    "DEFAULT_IPC_SERVER_NAME",
    "IGNORED_TEMP_EXTS",
    "SingleInstanceBridge",
    "WatchFolderService",
]
