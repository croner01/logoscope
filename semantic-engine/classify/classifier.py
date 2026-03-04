"""
Semantic Engine Classify 模块
负责事件分类，基于规则引擎进行事件类型识别
"""
from typing import Dict, Any, List


class EventClassifier:
    """
    事件分类器
    
    基于预定义的规则对事件进行分类，支持多种事件类型识别
    """
    
    def __init__(self):
        """
        初始化事件分类器
        
        设置默认的分类规则，包括数据库错误、网络错误、应用错误等
        """
        # 定义分类规则列表
        self.rules = [
            {
                "name": "database_error",
                "patterns": ["database", "mysql", "postgres", "connection", "timeout"],
                "level": "error"
            },
            {
                "name": "network_error",
                "patterns": ["network", "connection", "timeout", "socket"],
                "level": "error"
            },
            {
                "name": "application_error",
                "patterns": ["exception", "error", "crash", "fail"],
                "level": "error"
            },
            {
                "name": "warning",
                "patterns": ["warn", "warning", "deprecated"],
                "level": "warning"
            },
            {
                "name": "info",
                "patterns": ["info", "success", "start", "stop"],
                "level": "info"
            }
        ]
    
    def classify(self, event: Dict[str, Any]) -> str:
        """
        根据事件内容进行分类
        
        Args:
            event: 标准化后的事件，包含 event.raw 字段
                
        Returns:
            str: 事件类型名称，如果无法匹配则返回 "unknown"
        """
        # 获取事件原始内容并转换为小写
        raw_content = event.get("event", {}).get("raw", "").lower()
        
        # 遍历所有分类规则
        for rule in self.rules:
            # 检查是否匹配规则中的任何模式
            if any(pattern in raw_content for pattern in rule["patterns"]):
                # 返回匹配的规则名称
                return rule["name"]
        
        # 如果没有匹配的规则，返回 "unknown"
        return "unknown"


def classify_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """
    对事件进行分类
    
    Args:
        event: 标准化后的事件数据
        
    Returns:
        Dict[str, Any]: 添加了分类信息的事件，event.type 字段被更新为分类结果
    """
    # 创建事件分类器实例
    classifier = EventClassifier()
    
    # 对事件进行分类
    event_type = classifier.classify(event)
    
    # 更新事件的类型字段
    event["event"]["type"] = event_type
    
    # 返回更新后的事件
    return event
