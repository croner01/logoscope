"""
NATS connection test helpers shared by backend services.
"""

from __future__ import annotations

import asyncio
import ast
from typing import List, Sequence


def _normalize_servers_value(servers: str) -> Sequence[str]:
    """Normalize servers argument for nats-py connect."""
    raw = str(servers or "").strip()
    if not raw:
        return []

    if raw.startswith("[") and raw.endswith("]"):
        try:
            parsed = ast.literal_eval(raw)
            if isinstance(parsed, list):
                return [str(item) for item in parsed if str(item).strip()]
        except Exception:
            return [raw]

    return [raw]


async def test_nats_connection(servers: str) -> bool:
    """Test NATS connectivity using several connection styles."""
    print(f"[INFO]Testing NATS connection to: {servers}")

    try:
        from nats.aio.client import Client as NATSClient

        print("\n[TEST] Method 1: Direct connect")
        nc1 = NATSClient()
        try:
            await nc1.connect(servers=servers)
            print("[SUCCESS]Method 1 succeeded!")
            await nc1.close()
            return True
        except Exception as exc:
            print(f"[FAILED]Method 1 failed: {exc}")

        print("\n[TEST] Method 2: Connect with options")
        nc2 = NATSClient()
        try:
            await nc2.connect(
                servers=_normalize_servers_value(servers),
                connect_timeout=5,
                reconnect_time_wait=2,
                max_reconnect_attempts=1,
            )
            print("[SUCCESS]Method 2 succeeded!")
            await nc2.close()
            return True
        except Exception as exc:
            print(f"[FAILED]Method 2 failed: {exc}")

        print("\n[TEST] Method 3: Simple connect")
        nc3 = NATSClient()
        try:
            await nc3.connect()
            print("[SUCCESS]Method 3 succeeded (using default)!")
            await nc3.close()
            return True
        except Exception as exc:
            print(f"[FAILED]Method 3 failed: {exc}")

        return False

    except Exception as exc:
        print(f"[ERROR]NATS test failed: {exc}")
        import traceback

        traceback.print_exc()
        return False


async def main() -> None:
    """Script entrypoint for manual connectivity checks."""
    test_servers: List[str] = [
        "nats:4222",
        "nats://nats:4222",
        "nats://10.43.92.199:4222",
    ]

    print("=" * 60)
    print("NATS Connection Test")
    print("=" * 60)

    for servers in test_servers:
        print(f"\n{'=' * 60}")
        success = await test_nats_connection(servers)
        if success:
            print(f"\n[SUCCESS]Found working configuration: {servers}")
            break
        print(f"\n[FAILED]Configuration did not work: {servers}")

    print("\n" + "=" * 60)
    print("Test completed")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
