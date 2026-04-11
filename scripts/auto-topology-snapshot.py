#!/usr/bin/env python3
"""
自动拓扑快照脚本
定期保存服务拓扑状态，支持历史对比
"""

import os
import sys
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent / "semantic-engine"))

from graph.storage_adapter import StorageAdapter
from graph.topology_manager import TopologyManager

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def create_daily_snapshot(storage: StorageAdapter, time_window: str = "1 HOUR"):
    """创建每日拓扑快照"""
    try:
        topology_manager = TopologyManager(storage)
        
        # 获取当前拓扑
        topology = await topology_manager.get_hybrid_topology(time_window=time_window)
        
        # 生成快照名称
        snapshot_name = f"daily_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        # 保存快照
        snapshot_id = await topology_manager.create_snapshot(
            name=snapshot_name,
            time_window=time_window,
            description=f"自动快照 - {datetime.now().isoformat()}"
        )
        
        logger.info(f"快照创建成功: {snapshot_id} ({snapshot_name})")
        return snapshot_id
        
    except Exception as e:
        logger.error(f"创建快照失败: {e}")
        raise


async def cleanup_old_snapshots(storage: StorageAdapter, keep_days: int = 30):
    """清理旧快照（保留指定天数）"""
    try:
        topology_manager = TopologyManager(storage)
        
        # 获取所有快照
        snapshots = await topology_manager.get_all_snapshots()
        
        cutoff_date = datetime.now() - timedelta(days=keep_days)
        deleted_count = 0
        
        for snapshot in snapshots:
            snapshot_time = datetime.fromisoformat(snapshot.get('created_at', ''))
            if snapshot_time < cutoff_date:
                await topology_manager.delete_snapshot(snapshot['id'])
                deleted_count += 1
                logger.info(f"删除旧快照: {snapshot['name']} ({snapshot['id']})")
        
        logger.info(f"清理完成: 删除了 {deleted_count} 个旧快照")
        
    except Exception as e:
        logger.error(f"清理快照失败: {e}")


async def compare_snapshots(storage: StorageAdapter, snapshot_id_1: str, snapshot_id_2: str):
    """对比两个快照，发现拓扑变化"""
    try:
        topology_manager = TopologyManager(storage)
        
        diff = await topology_manager.compare_snapshots(snapshot_id_1, snapshot_id_2)
        
        logger.info(f"快照对比结果:")
        logger.info(f"  新增节点: {len(diff.get('added_nodes', []))}")
        logger.info(f"  删除节点: {len(diff.get('removed_nodes', []))}")
        logger.info(f"  新增边: {len(diff.get('added_edges', []))}")
        logger.info(f"  删除边: {len(diff.get('removed_edges', []))}")
        
        return diff
        
    except Exception as e:
        logger.error(f"对比快照失败: {e}")
        raise


async def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description="自动拓扑快照管理")
    parser.add_argument("--action", choices=["snapshot", "cleanup", "compare"], 
                       default="snapshot", help="执行的操作")
    parser.add_argument("--time-window", default="1 HOUR", 
                       help="时间窗口（如: 1 HOUR, 6 HOUR, 1 DAY）")
    parser.add_argument("--keep-days", type=int, default=30,
                       help="保留快照天数（用于cleanup）")
    parser.add_argument("--snapshot-1", help="对比的快照ID 1（用于compare）")
    parser.add_argument("--snapshot-2", help="对比的快照ID 2（用于compare）")
    
    args = parser.parse_args()
    
    # 初始化存储适配器
    storage = StorageAdapter()
    
    if args.action == "snapshot":
        await create_daily_snapshot(storage, args.time_window)
        
    elif args.action == "cleanup":
        await cleanup_old_snapshots(storage, args.keep_days)
        
    elif args.action == "compare":
        if not args.snapshot_1 or not args.snapshot_2:
            logger.error("--compare 操作需要 --snapshot-1 和 --snapshot-2 参数")
            sys.exit(1)
        await compare_snapshots(storage, args.snapshot_1, args.snapshot_2)


if __name__ == "__main__":
    asyncio.run(main())
