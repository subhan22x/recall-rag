"""Stable absolute-path entry point for desktop MCP clients."""

from backend.app.mcp_server import run_stdio


if __name__ == "__main__":
    run_stdio()
