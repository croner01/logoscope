"""SSH Executor — 通过 SSH 执行命令。"""


class SSHExecutor:
    """SSH 执行器——在生产中通过 exec-service 调用。"""
    def __init__(self, config: dict = None):
        self.config = config or {}

    def execute(self, command: str, host: str) -> dict:
        # 生产环境：通过 SSH 执行
        return {"host": host, "command": command, "status": "pending"}
