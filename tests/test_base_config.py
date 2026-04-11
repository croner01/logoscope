"""
测试 shared_src/config/base.py - BaseConfig
"""
import os
import pytest
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared_src'))

from config.base import BaseConfig


class TestBaseConfig:
    """测试 BaseConfig 类"""

    def test_default_values(self):
        """测试默认值"""
        config = BaseConfig(app_name="test-service", default_port=9000)
        
        assert config.app_name == "test-service"
        assert config.port == 9000
        assert config.host == "0.0.0.0"
        assert config.app_version == "1.0.0"
        assert config.debug is False

    def test_env_override_app_name(self, monkeypatch):
        """测试环境变量覆盖 app_name"""
        monkeypatch.setenv("APP_NAME", "env-service")
        config = BaseConfig(app_name="default-service")
        
        assert config.app_name == "env-service"

    def test_env_override_port(self, monkeypatch):
        """测试环境变量覆盖端口"""
        monkeypatch.setenv("PORT", "9999")
        config = BaseConfig(default_port=8080)
        
        assert config.port == 9999

    def test_parse_port_simple(self):
        """测试简单端口解析"""
        assert BaseConfig._parse_port("9000") == 9000
        assert BaseConfig._parse_port("8080") == 8080

    def test_parse_port_url_format(self):
        """测试 URL 格式端口解析"""
        assert BaseConfig._parse_port("tcp://localhost:9000") == 9000
        assert BaseConfig._parse_port("bolt://neo4j:7687") == 7687

    def test_clickhouse_config(self, monkeypatch):
        """测试 ClickHouse 配置"""
        monkeypatch.setenv("CLICKHOUSE_HOST", "ch-server")
        monkeypatch.setenv("CLICKHOUSE_PORT", "9001")
        monkeypatch.setenv("CLICKHOUSE_DATABASE", "test_db")
        
        config = BaseConfig()
        ch_config = config.get_clickhouse_config()
        
        assert ch_config["host"] == "ch-server"
        assert ch_config["port"] == 9001
        assert ch_config["database"] == "test_db"

    def test_neo4j_config(self, monkeypatch):
        """测试 Neo4j 配置"""
        monkeypatch.setenv("NEO4J_HOST", "neo4j-server")
        monkeypatch.setenv("NEO4J_PORT", "7688")
        
        config = BaseConfig()
        neo4j_config = config.get_neo4j_config()
        
        assert neo4j_config["host"] == "neo4j-server"
        assert neo4j_config["port"] == 7688

    def test_storage_config(self):
        """测试完整存储配置"""
        config = BaseConfig()
        storage_config = config.get_storage_config()
        
        assert "clickhouse" in storage_config
        assert "neo4j" in storage_config
        assert "host" in storage_config["clickhouse"]
        assert "host" in storage_config["neo4j"]
