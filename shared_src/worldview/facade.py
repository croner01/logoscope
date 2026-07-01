"""WorldView Facade——统一查询入口。

v15: 纯 Facade 模式——组合 TopologyQuery + StateQuery + HistoryQuery。
     不负责任何查询实现。新增查询能力 = 新增 Query 类，不膨胀 WorldView。
"""


class WorldView:
    """
    世界视图——AI 组件的统一查询入口。

    纯 Facade 模式：
    - topology: TopologyQuery → 拓扑/依赖/影响范围
    - state: StateQuery → 当前状态/演化链
    - history: HistoryQuery → 历史事件/告警
    """

    def __init__(self, topology, state, history):
        self.topology = topology
        self.state = state
        self.history = history
