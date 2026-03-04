"""
Semantic Engine Relation 模块
负责从事件中抽取服务依赖关系
"""
from typing import Dict, Any, List


class RelationExtractor:
    """
    关系抽取器
    
    基于预定义的规则从事件内容中抽取服务依赖关系
    """
    
    def __init__(self):
        """
        初始化关系抽取器
        
        设置默认的关系抽取规则，包括 depends_on、calls、error 等关系类型
        """
        # 定义关系抽取规则列表
        self.rules = [
            {
                "name": "depends_on",
                "patterns": ["connect", "depend", "rely", "use"],
                "targets": ["mysql", "postgres", "redis", "kafka", "database", "service"]
            },
            {
                "name": "calls",
                "patterns": ["call", "invoke", "request", "api"],
                "targets": ["service", "api", "endpoint"]
            },
            {
                "name": "error",
                "patterns": ["error", "fail", "exception"],
                "targets": ["service", "database", "network"]
            }
        ]
    
    def extract(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        从事件中抽取关系
        
        Args:
            event: 待抽取关系的事件数据
                
        Returns:
            List[Dict[str, Any]]: 抽取的关系列表，每个关系包含：
                - type: 关系类型
                - source: 源服务
                - target: 目标服务
                - timestamp: 时间戳
        """
        # 初始化关系列表
        relations = []
        
        # 获取事件原始内容并转换为小写
        raw_content = event.get("event", {}).get("raw", "").lower()
        
        # 获取源服务名
        source_service = event.get("entity", {}).get("name")
        
        # 遍历所有关系抽取规则
        for rule in self.rules:
            # 检查是否匹配规则中的任何模式
            if any(pattern in raw_content for pattern in rule["patterns"]):
                # 遍历所有目标类型
                for target in rule["targets"]:
                    # 检查是否包含目标类型
                    if target in raw_content:
                        # 添加关系到列表
                        relations.append({
                                "type": rule["name"],
                                "source": source_service,
                                "target": target,
                                "timestamp": event.get("timestamp")
                        })
        
        # 返回抽取的关系列表
        return relations


def extract_relations(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    从事件中抽取关系
    
    Args:
        event: 待抽取关系的事件数据
        
    Returns:
        Dict[str, Any]: 添加了关系信息的事件，relations 字段被更新
    """
    # 创建关系抽取器实例
    extractor = RelationExtractor()
    
    # 从事件中抽取关系
    relations = extractor.extract(event)
    
    # 更新事件的关系字段
    event["relations"] = relations
    
    # 返回添加了关系信息的事件
    return event
