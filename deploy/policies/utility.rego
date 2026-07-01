package logoscope.policy

# Utility 权重通过 data.logoscope.utility_weights 注入
default utility = 0

utility = score {
    w := data.logoscope.utility_weights
    score := (input.candidate.estimated_success_rate * 100 * w.success)
           - (input.candidate.risk.final_risk * w.risk)
           - (input.candidate.estimated_duration_minutes * w.cost)
           - (input.candidate.blast.vm_count * w.blast)
}
