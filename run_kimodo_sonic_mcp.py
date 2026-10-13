#!/usr/bin/env python3
"""启动 Kimodo + SONIC 的远程 MCP 控制服务。"""

from __future__ import annotations

import sys
from pathlib import Path

from kimodo_sonic_service.config import ServiceConfig
from kimodo_sonic_service.mcp_server import create_mcp_server


def main() -> int:
    try:
        config = ServiceConfig.from_env(Path(__file__).resolve().parent)
        server = create_mcp_server(config)
        server.run(transport="streamable-http")
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
