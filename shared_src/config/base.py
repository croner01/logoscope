"""
Logoscope 基础配置类
所有服务共享的配置逻辑
"""
import os
from typing import Any, Dict


class BaseConfig:
    """
    基础配置类
    
    从环境变量加载配置，提供默认值
    各服务继承此类并添加服务特有配置
    """
    
    def __init__(
        self,
        app_name: str = "logoscope-service",
        default_port: int = 8080
    ):
        """
        初始化配置

        Args:
            app_name: 服务名称（用于默认值和日志）
            default_port: 默认端口
        """
        self._init_app_config(app_name, default_port)
        self._init_clickhouse_config()
        self._init_neo4j_config()
        self._init_log_config()
    
    def _init_app_config(self, app_name: str, default_port: int):
        """初始化应用配置"""
        self.app_name = os.getenv("APP_NAME", app_name)
        self.app_version = os.getenv("APP_VERSION", "1.0.0")
        self.host = os.getenv("HOST", "0.0.0.0")
        self.port = int(os.getenv("PORT", str(default_port)))
        self.debug = os.getenv("DEBUG", "false").lower() == "true"
    
    def _init_clickhouse_config(self):
        """初始化 ClickHouse 配置"""
        self.clickhouse_host = os.getenv("CLICKHOUSE_HOST", "clickhouse")
        self.clickhouse_port = self._parse_port(os.getenv("CLICKHOUSE_PORT", "9000"))
        self.clickhouse_database = os.getenv("CLICKHOUSE_DATABASE", "logs")
        self.clickhouse_user = os.getenv("CLICKHOUSE_USER", "default")
        self.clickhouse_password = os.getenv("CLICKHOUSE_PASSWORD", "")
    
    def _init_neo4j_config(self):
        """初始化 Neo4j 配置"""
        self.neo4j_host = os.getenv("NEO4J_HOST", "neo4j")
        self.neo4j_port = self._parse_port(os.getenv("NEO4J_PORT", "7687"))
        self.neo4j_user = os.getenv("NEO4J_USER", "neo4j")
        self.neo4j_password = os.getenv("NEO4J_PASSWORD", "password")
        self.neo4j_database = os.getenv("NEO4J_DATABASE", "neo4j")
    
    def _init_log_config(self):
        """初始化日志配置"""
        self.log_level = os.getenv("LOG_LEVEL", "info")
    
    @staticmethod
    def _parse_port(port_str: str) -> int:
        """
        从环境变量解析端口号，处理 URL 格式
        
        Args:
            port_str: 端口字符串，可能是 "9000" 或 "tcp://host:9000"
            
        Returns:
            int: 端口号
        """
        if '://' in port_str:
            port_str = port_str.split(':')[-1]
        return int(port_str)
    
    def get_clickhouse_config(self) -> Dict[str, Any]:
        """获取 ClickHouse 连接配置"""
        return {
            "host": self.clickhouse_host,
            "port": self.clickhouse_port,
            "database": self.clickhouse_database,
            "user": self.clickhouse_user,
            "password": self.clickhouse_password
        }
    
    def get_neo4j_config(self) -> Dict[str, Any]:
        """获取 Neo4j 连接配置"""
        return {
            "host": self.neo4j_host,
            "port": self.neo4j_port,
            "user": self.neo4j_user,
            "password": self.neo4j_password,
            "database": self.neo4j_database
        }
    
    def get_storage_config(self) -> Dict[str, Any]:
        """获取完整存储配置（ClickHouse + Neo4j）"""
        return {
            "clickhouse": self.get_clickhouse_config(),
            "neo4j": self.get_neo4j_config()
        }
    
    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"app_name={self.app_name} "
            f"port={self.port}>"
        )
