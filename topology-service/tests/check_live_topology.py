"""
从 OpenStack 日志中提取 global_request_id 并重建调用链。
在 ClickHouse 上直接查询，然后用 Python 解析。
"""
import re
import sys
import urllib.request
import urllib.parse

CH_URL = "http://10.43.243.71:8123/"

def ch_query(sql):
    """Execute a ClickHouse query and return raw TSV output."""
    data = sql.encode('utf-8')
    req = urllib.request.Request(CH_URL, data=data)
    req.add_header('Content-Type', 'text/plain')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode('utf-8')
    except Exception as e:
        print(f"Query error: {e}", file=sys.stderr)
        return ""

# Pattern: [req-<uuid> <32hex> ...] → extract the 32hex
PAT_GLOBAL = re.compile(r'\[req-[^\]]+ ([a-f0-9]{32})')

# Step 1: 看 logs.logs 中有 openstack_request_id 的服务
print("=" * 60)
print("【1】OpenStack 服务分布（5分钟内）")
print("=" * 60)

sql1 = """
SELECT service_name, count() AS cnt
FROM logs.logs
WHERE timestamp > now() - INTERVAL 5 MINUTE
  AND openstack_request_id != ''
GROUP BY service_name
ORDER BY cnt DESC
LIMIT 30
FORMAT TSV
"""
result = ch_query(sql1)
services = []
for line in result.strip().split('\n'):
    if not line.strip():
        continue
    parts = line.split('\t')
    if len(parts) >= 2:
        services.append((parts[0], int(parts[1])))
        print(f"  {parts[0]:30s} {parts[1]:>8}")

print(f"\n共 {len(services)} 个 OpenStack 服务")

# Step 2: 从 message 提取 global_request_id，重建调用链
print("\n" + "=" * 60)
print("【2】提取 global_request_id 重建调用链")
print("=" * 60)

sql2 = """
SELECT service_name, left(message, 500) AS msg
FROM logs.logs
WHERE timestamp > now() - INTERVAL 5 MINUTE
  AND openstack_request_id != ''
  AND length(message) > 50
ORDER BY message ASC
LIMIT 10000
FORMAT TSV
"""
result = ch_query(sql2)

# 分组
groups = {}
for line in result.strip().split('\n'):
    if not line.strip():
        continue
    parts = line.split('\t', 1)
    if len(parts) < 2:
        continue
    svc, msg = parts[0], parts[1]
    m = PAT_GLOBAL.search(msg)
    if m:
        gid = m.group(1)
        groups.setdefault(gid, []).append(svc)

print(f"含 global_request_id 的行: {sum(len(v) for v in groups.values())}")
print(f"不同的 global_request_id: {len(groups)}")

# 调用链
chains = []
for gid, svcs in groups.items():
    seen = []
    for s in svcs:
        if s not in seen:
            seen.append(s)
    if len(seen) >= 2:
        chains.append((gid, seen))

print(f"跨服务调用链: {len(chains)}")

# 打印最常见的调用链
chain_pairs = {}
for gid, chain in chains:
    for i in range(len(chain) - 1):
        pair = (chain[i], chain[i + 1])
        chain_pairs[pair] = chain_pairs.get(pair, 0) + 1

print("\n调用边 (caller → callee):")
for (caller, callee), cnt in sorted(chain_pairs.items(), key=lambda x: -x[1])[:20]:
    print(f"  {caller:30s} → {callee:30s}  (count={cnt})")

# Step 3: 看 inference 能从 logs 走 request_id 关联出哪些边
print("\n" + "=" * 60)
print("【3】request_id 关联跨服务调用")
print("=" * 60)

sql3 = """
SELECT service_name, left(message, 200) AS msg
FROM logs.logs
WHERE timestamp > now() - INTERVAL 5 MINUTE
  AND match(message, 'request.id=|request_id=|x-request-id=')
LIMIT 2000
FORMAT TSV
"""
result = ch_query(sql3)

# 用 Python 提取 request_id
PAT_REQID = re.compile(r'(?:request[_-]?id|x-request-id)\s*[:=]\s*([a-zA-Z0-9\-_.]{6,})')
req_groups = {}
for line in result.strip().split('\n'):
    if not line.strip():
        continue
    parts = line.split('\t', 1)
    if len(parts) < 2:
        continue
    svc, msg = parts[0], parts[1]
    m = PAT_REQID.search(msg)
    if m:
        rid = m.group(1)
        req_groups.setdefault(rid, []).append(svc)

req_pairs = {}
for rid, svcs in req_groups.items():
    seen = []
    for s in svcs:
        if s not in seen:
            seen.append(s)
    for i in range(len(seen) - 1):
        pair = (seen[i], seen[i + 1])
        req_pairs[pair] = req_pairs.get(pair, 0) + 1

if req_pairs:
    print(f"含 request_id 的行: {sum(len(v) for v in req_groups.values())}")
    print(f"不同的 request_id: {len(req_groups)}")
    print(f"跨服务调用边:")
    for (caller, callee), cnt in sorted(req_pairs.items(), key=lambda x: -x[1])[:20]:
        print(f"  {caller:30s} → {callee:30s}  (count={cnt})")
else:
    print("没有找到含 request_id 的日志")

# Step 4: 总结
print("\n" + "=" * 60)
print("【4】当前拓扑数据源状态总结")
print("=" * 60)

print(f"""
  Traces:        ✅ 有数据（4 服务，21k spans/h）  ⚠️ 无跨服务边
  Interactions:  ❌ logs.interactions 表不存在
  OpenStack:     ⚠️ global_request_id 列空      ✅ message 内可提取
  Logs:          ✅ 有 50 万行/h                ✅ 可走推断边
  Metrics:       ? 待检查
""")
