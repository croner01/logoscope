"""GoalInferrer — Finding → Goal Tree（目标状态树）。"""
from typing import Optional
from shared_src.goal.models import GoalNode, Goal


class GoalInferrer:
    """
    Goal Inferrer——从 Finding 推断目标状态树。

    - RabbitMQHeartbeatLost → restore_messaging
    - 低置信度 → collect_evidence
    """

    def infer(self, finding, worldview) -> Optional[Goal]:
        category = finding.category if hasattr(finding, "category") else ""
        affected = finding.affected_entities if hasattr(finding, "affected_entities") else []

        if not affected:
            return Goal(
                primary="collect_evidence",
                tree=GoalNode(goal_id="root", desired_state="evidence_collected",
                              entity_type="UNKNOWN", entity_name="unknown"),
                priority=30,
            )

        first_entity = affected[0] if affected else "unknown:unknown"
        parts = first_entity.split(":", 1)
        entity_type = parts[0] if len(parts) > 1 else "SERVICE"
        entity_name = parts[1] if len(parts) > 1 else parts[0]

        if "Heartbeat" in category or "heartbeat" in category:
            return Goal(
                primary="restore_messaging",
                tree=GoalNode(
                    goal_id="root",
                    desired_state="Cluster.healthy",
                    entity_type=entity_type,
                    entity_name=entity_name,
                    children=[
                        GoalNode(goal_id="svc", desired_state=f"{entity_name}.healthy",
                                  entity_type=entity_type, entity_name=entity_name),
                        GoalNode(goal_id="nova", desired_state="NovaAPI.responding",
                                  entity_type="SERVICE", entity_name="nova-api"),
                    ],
                ),
                priority=90,
                reason=f"{category} → restore messaging cluster",
            )

        if finding.confidence < 0.5:
            return Goal(
                primary="collect_evidence",
                tree=GoalNode(goal_id="root", desired_state="evidence_collected",
                              entity_type=entity_type, entity_name=entity_name),
                priority=40,
                reason=f"Low confidence ({finding.confidence}) → collect evidence",
            )

        return Goal(
            primary="restore_service",
            tree=GoalNode(goal_id="root", desired_state=f"{entity_name}.healthy",
                          entity_type=entity_type, entity_name=entity_name),
            priority=50,
        )
