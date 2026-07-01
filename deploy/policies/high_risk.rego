package logoscope.policy

# 基于 risk 的通用规则（不依赖 Capability 名称）
decision = "deny" {
    input.candidate.final_risk >= 80
}

decision = "pending_approval" {
    input.candidate.final_risk >= 40
    input.candidate.final_risk < 80
}

decision = "allow" {
    input.candidate.final_risk < 40
}
