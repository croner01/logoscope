#!/usr/bin/env python3
"""
简单的语法验证脚本
"""
import sys
import os

print("Verifying Python syntax...")

files_to_check = [
    "services/protobuf_parser.py",
    "api/ingest.py"
]

errors_found = False

for filename in files_to_check:
    filepath = os.path.join(os.path.dirname(__file__), filename)
    print(f"\nChecking {filename}...")
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            source = f.read()
        compile(source, filename, 'exec')
        print(f"✅ {filename} - syntax OK")
    except Exception as e:
        print(f"❌ {filename} - syntax ERROR: {e}")
        errors_found = True

print("\n" + "="*60)
if errors_found:
    print("❌ Syntax verification FAILED")
    sys.exit(1)
else:
    print("✅ All files passed syntax verification!")
    sys.exit(0)
