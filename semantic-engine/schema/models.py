import uuid
from pydantic import BaseModel, Field
from typing import Dict, Any, List, Optional
from datetime import datetime

class Entity(BaseModel):
    """实体模型"""
    type: str = Field(default="service")
    name: str = Field(default="unknown")
    instance: str = Field(default="unknown")

class EventDetail(BaseModel):
    """事件详情模型"""
    type: str = Field(default="log")
    level: str = Field(default="info")
    name: str = Field(default="unknown")
    raw: str = Field(default="")

class Context(BaseModel):
    """上下文模型"""
    trace_id: str = Field(default="")
    span_id: str = Field(default="")
    host: str = Field(default="unknown")
    k8s: Dict[str, Any] = Field(default_factory=dict)

class Relation(BaseModel):
    """关系模型"""
    type: str
    source: str
    target: str
    timestamp: Optional[str] = None

class EventModel(BaseModel):
    """事件模型"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())
    entity: Entity = Field(default_factory=Entity)
    event: EventDetail = Field(default_factory=EventDetail)
    context: Context = Field(default_factory=Context)
    relations: List[Relation] = Field(default_factory=list)
    correlations: Optional[List[Dict[str, Any]]] = Field(default_factory=list)
