"""
自定义异常体系 — 解压引擎与调度专用异常
"""
class ShellBreakerError(Exception):
    """所有 ShellBreaker 异常的基类。"""
    pass


class PasswordRequiredError(ShellBreakerError):
    """引擎报告需要密码（未提供密码时），绝不允许死锁等待用户输入。"""
    def __init__(self, archive: str, engine_output: str = ""):
        self.archive = archive
        self.engine_output = engine_output
        super().__init__(f"密码缺失，引擎要求提供密码: {archive}")


class WrongPasswordError(ShellBreakerError):
    """提供的密码错误——与 PasswordRequiredError 严格区分，防止误入 Fallback 链。"""
    def __init__(self, archive: str, password_used: str = "", engine_output: str = ""):
        self.archive = archive
        self.password_used = password_used
        self.engine_output = engine_output
        super().__init__(f"密码错误: {archive}")


class ZipBombDetectedError(ShellBreakerError):
    """嵌套解压深度超限（>50 层），疑似 Zip Bomb。"""
    def __init__(self, archive: str, depth: int):
        self.archive = archive
        self.depth = depth
        super().__init__(f"Zip Bomb 防御触发：嵌套深度 {depth} > 50，中断解压: {archive}")


class EngineNotFoundError(ShellBreakerError):
    """未找到可用的解压引擎 exe。"""
    def __init__(self, engine_name: str):
        self.engine_name = engine_name
        super().__init__(f"未找到解压引擎: {engine_name}")


class ExtractionFailedError(ShellBreakerError):
    """解压引擎返回非零退出码。"""
    def __init__(self, archive: str, returncode: int, stderr: str = ""):
        self.archive = archive
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"解压失败 (exit={returncode}): {archive}")


class SandboxError(ShellBreakerError):
    """沙箱创建或文件操作失败。"""
    def __init__(self, message: str):
        super().__init__(f"沙箱错误: {message}")