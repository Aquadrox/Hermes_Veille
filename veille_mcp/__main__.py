"""veille-mcp — serveur MCP (stdio) des veilles.

  veille-mcp               # serveur stdio (lancé par Hermes)
  veille-mcp --readonly    # outils de lecture uniquement (aussi : VEILLE_MCP_READONLY=true)
  veille-mcp --check       # liste les outils exposés et sort (diagnostic)
"""
import argparse
import asyncio
import json
import logging
import os
import sys

from veille_core import version

from .server import build_server


def main() -> int:
    ap = argparse.ArgumentParser(prog="veille-mcp", description="Serveur MCP des veilles (stdio)")
    ap.add_argument("--readonly", action="store_true", help="n'exposer que les outils de lecture")
    ap.add_argument("--check", action="store_true", help="afficher les outils exposés puis sortir")
    ap.add_argument("--version", action="version", version=f"veille-mcp {version()}")
    args = ap.parse_args()
    readonly = args.readonly or os.environ.get("VEILLE_MCP_READONLY", "").lower() in ("1", "true", "yes")
    # stdout est réservé au protocole : tous les journaux vont sur stderr.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING)
    server = build_server(readonly=readonly)
    if args.check:
        tools = asyncio.run(server.list_tools())
        print(json.dumps({"version": version(), "readonly": readonly,
                          "tools": sorted(t.name for t in tools)}, ensure_ascii=False))
        return 0
    server.run("stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
