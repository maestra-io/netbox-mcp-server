"""Entry point that adds the write tools to the upstream server.

Run as ``python -m netbox_mcp_server.writes`` instead of the upstream
``netbox-mcp-server`` console script. Registration happens before upstream's
main() builds the NetBox client and starts the transport, so everything after
it is unmodified upstream behaviour. Cloudflare Access mode (access.py), when
its env is set, is switched on first, then the optional agent ``reason`` on
every tool (reason.py); see FORK.md.

Deliberately NOT wired into the image's CMD or into pyproject's
[project.scripts]: leaving every upstream file untouched is what makes upstream
merges conflict-free. The deployment selects this entry point instead.
"""

import os

from netbox_mcp_server import server as upstream
from netbox_mcp_server.config import Settings
from netbox_mcp_server.server import main, parse_cli_args
from netbox_mcp_server.writes import access, reason, write_tools


def _transport_settings() -> tuple[str | None, bool]:
    """(transport, MCP_AUTH_TOKEN set) as upstream main() will see them."""
    try:
        settings = Settings(**parse_cli_args())
    except Exception:  # main() reports the same error itself
        return None, False
    return settings.transport, settings.mcp_auth_token is not None


def run() -> None:
    access.enable_from_env(os.environ, upstream.mcp, _transport_settings)
    upstream.mcp.add_middleware(reason.AgentReasonMiddleware())
    write_tools.register()
    main()


if __name__ == "__main__":
    run()
