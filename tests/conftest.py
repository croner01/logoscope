"""
pytest 配置和共享 fixtures
"""
import os
import sys

# 添加 shared_src 到 Python 路径
shared_src_path = os.path.join(os.path.dirname(__file__), '..', 'shared_src')
if os.path.exists(shared_src_path):
    sys.path.insert(0, shared_src_path)

# 添加各服务目录到 Python 路径
service_paths = [
    os.path.join(os.path.dirname(__file__), '..', 'semantic-engine'),
    os.path.join(os.path.dirname(__file__), '..', 'query-service'),
    os.path.join(os.path.dirname(__file__), '..', 'topology-service'),
]
for path in service_paths:
    if os.path.exists(path):
        sys.path.insert(0, path)
