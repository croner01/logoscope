"""
Semantic Engine Correlate 模块
负责跨源事件关联，基于 trace_id 和服务名进行关联分析
"""
from typing import Dict, Any, List
from collections import defaultdict


class EventCorrelator:
    """
    事件关联器
    
    基于多种维度进行事件关联，包括 trace_id 和服务名
    """
    
    def __init__(self):
        """
        初始化事件关联器
        
        初始化用于存储事件的字典，按 trace_id 和服务名索引
        """
        # 按 trace_id 存储事件列表
        self.events_by_trace = defaultdict(list)
        # 按服务名存储事件列表
        self.events_by_service = defaultdict(list)
    
    def add_event(self, event: Dict[str, Any]):
        """
        添加事件到关联器
        
        Args:
            event: 待关联的事件数据
        """
        # 获取 trace_id
        trace_id = event.get("context", {}).get("trace_id")
        # 获取服务名
        service_name = event.get("entity", {}).get("name")
        
        # 如果有 trace_id，按 trace_id 索引
        if trace_id:
            self.events_by_trace[trace_id].append(event)
        
        # 如果有服务名，按服务名索引
        if service_name:
            self.events_by_service[service_name].append(event)
    
    def correlate(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        关联当前事件与其他事件
        
        Args:
            event: 待关联的事件数据
                
        Returns:
            List[Dict[str, Any]]: 关联的事件列表
        """
        # 初始化关联事件列表
        correlated_events = []
        
        # 获取当前事件的 trace_id
        trace_id = event.get("context", {}).get("trace_id")
        
        # 按 trace_id 关联事件
        if trace_id:
            for related_event in self.events_by_trace.get(trace_id, []):
                # 排除自身
                if related_event["id"] != event["id"]:
                    correlated_events.append(related_event)
        
        # 获取当前事件的服务名
        service_name = event.get("entity", {}).get("name")
        
        # 按服务名关联最近的事件（最近5个）
        if service_name:
            recent_events = self.events_by_service.get(service_name, [])[-5:]
            for related_event in recent_events:
                # 排除自身和已关联的事件
                if related_event["id"] != event["id"] and related_event not in correlated_events:
                    correlated_events.append(related_event)
        
        # 返回关联的事件列表
        return correlated_events


def correlate_events(event: Dict[str, Any], all_events: List[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    对事件进行跨源关联
    
    Args:
        event: 待关联的事件
        all_events: 可选的事件列表，用于关联（如果为 None 则只关联当前事件）
        
    Returns:
        Dict[str, Any]: 添加了关联信息的事件，包含 correlations 字段
    """
    # 创建事件关联器实例
    correlator = EventCorrelator()
    
    # 如果提供了事件列表，先添加到关联器
    if all_events:
        for e in all_events:
            correlator.add_event(e)
    
    # 添加当前事件到关联器
    correlator.add_event(event)
    
    # 执行关联
    correlated_events = correlator.correlate(event)
    
    # 构建关联信息列表
    event["correlations"] = [
        {
                "event_id": e["id"],
                "service": e["entity"]["name"],
                "event_type": e["event"]["type"]
        }
        for e in correlated_events
    ]
    
    # 返回添加了关联信息的事件
    return event
