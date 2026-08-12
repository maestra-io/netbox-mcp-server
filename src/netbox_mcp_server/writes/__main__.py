"""Entry point that adds the write tools to the upstream server.

Run as ``python -m netbox_mcp_server.writes`` instead of the upstream
``netbox-mcp-server`` console script. Registration happens before upstream's
main() builds the NetBox client and starts the transport, so everything after
it is unmodified upstream behaviour.

Deliberately NOT wired into the image's CMD or into pyproject's
[project.scripts]: leaving every upstream file untouched is what makes upstream
merges conflict-free. The deployment selects this entry point instead.
"""

from netbox_mcp_server.server import main
from netbox_mcp_server.writes import write_tools


def run() -> None:
    write_tools.register()
    main()


if __name__ == "__main__":
    run()
