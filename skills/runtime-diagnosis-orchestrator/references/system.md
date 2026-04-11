# System/Host Playbook

Use this reference for node or host-level troubleshooting.

## Focus Areas
- CPU, memory, IO saturation
- Process and socket pressure
- Disk, filesystem, kernel/network anomalies

## Typical Evidence Questions
- Is host pressure propagating to pod/service failures?
- Are kernel or cgroup limits causing throttling or kill events?
- Is local DNS/network stack unstable?

## Read-Only Command Patterns
- `top -b -n 1` / `ps -eo ...`
- `dmesg --ctime | tail -n <n>`
- `ss -s` / `netstat -s`
- `iostat -x <interval> <count>`
- `df -h` / `free -m`

## Correlation Keys
- host timestamp alignment with app error timestamp
- node hostname/IP to kubernetes node mapping
- process/container ID to workload identity

## Guardrails
- Always bound command runtime and output size.
- Avoid interactive commands.
- Keep system diagnostics read-only unless approval is explicit.
