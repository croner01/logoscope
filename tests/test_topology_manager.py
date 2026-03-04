"""
Topology Manager 单元测试
"""

import unittest
import sys
import asyncio
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent / "semantic-engine"))

from graph.topology_manager import TopologyManager
from graph.storage_adapter import StorageAdapter


class TestTopologyManager(unittest.TestCase):
    """拓扑管理器测试"""
    
    def setUp(self):
        """测试前设置"""
        self.storage = StorageAdapter()
    
    def test_get_hybrid_topology(self):
        """测试混合拓扑获取"""
        async def run_test():
            manager = TopologyManager(self.storage)
            topology = await manager.get_hybrid_topology(time_window="1 HOUR")
            
            self.assertIn("nodes", topology)
            self.assertIn("edges", topology)
            self.assertIsInstance(topology["nodes"], list)
            self.assertIsInstance(topology["edges"], list)
        
        asyncio.run(run_test())
    
    def test_create_snapshot(self):
        """测试快照创建"""
        async def run_test():
            manager = TopologyManager(self.storage)
            snapshot_id = await manager.create_snapshot(
                name="test_snapshot",
                time_window="1 HOUR"
            )
            self.assertIsNotNone(snapshot_id)
        
        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()
