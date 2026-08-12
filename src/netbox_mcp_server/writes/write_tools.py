"""Write tools for the NetBox MCP server.

Upstream is deliberately read-only: it registers four read tools on its FastMCP
instance, even though the NetBoxRestClient it ships already implements
create/update/delete. This module registers the missing write tools on that
*same* instance, so this fork stays a set of added files rather than a patched
copy of upstream.

Safety properties, in the order they matter:

* Writes are OFF unless ``NETBOX_MCP_ENABLE_WRITES`` is truthy. The image is
  therefore read-only by default; only a deployment that opts in gets the
  tools, and a deployment that does not opt in does not even advertise them.
* ``netbox_update_object`` issues PATCH, never PUT, so fields the caller did
  not mention are left alone.
* ``netbox_delete_object`` requires the caller to echo the object's current
  ``display`` string. A wrong id then fails loudly instead of deleting the
  wrong object, which is the failure mode that cannot be undone from the
  NetBox changelog.
* Every tool returns the object state it produced (or, for delete, the
  pre-delete state), so the caller can verify instead of assuming.

NetBox permissions are the real enforcement layer: the token's user needs
add/change/delete object permissions on the relevant object types, or these
tools get a 403 no matter what the token's write flag says.
"""

import contextlib
import json
import logging
import os
import sys
from typing import Any

import httpx

from netbox_mcp_server import server as upstream

logger = logging.getLogger(__name__)

ENABLE_WRITES_ENV = "NETBOX_MCP_ENABLE_WRITES"

_TRUTHY = {"1", "true", "yes", "on"}


def writes_enabled() -> bool:
    """Whether write tools should be registered."""
    return os.environ.get(ENABLE_WRITES_ENV, "").strip().lower() in _TRUTHY


def _resolve_endpoint(object_type: str) -> str:
    """Map a NetBox object type to its API endpoint, or raise with the valid list."""
    if object_type not in upstream.NETBOX_OBJECT_TYPES:
        valid_types = "\n".join(f"- {t}" for t in sorted(upstream.NETBOX_OBJECT_TYPES))
        raise ValueError(f"Invalid object_type. Must be one of:\n{valid_types}")
    endpoint, _fallback = upstream._get_endpoint_info(object_type)
    return endpoint


def _client() -> Any:
    """The NetBox client the upstream server builds in main()."""
    if upstream.netbox is None:
        raise RuntimeError("NetBox client is not initialised")
    return upstream.netbox


def _describe_http_error(exc: httpx.HTTPStatusError) -> str:
    """Turn a NetBox error response into something a caller can act on.

    NetBox puts the useful part (per-field validation errors, 'permission
    denied') in the response body; the bare status line does not say which
    field was rejected.
    """
    body = exc.response.text.strip()
    with contextlib.suppress(ValueError):
        body = json.dumps(json.loads(body), ensure_ascii=False)
    status = exc.response.status_code
    hint = ""
    if status == 403:
        hint = " (the token's NetBox user lacks add/change/delete permission for this object type)"
    return f"NetBox returned HTTP {status}{hint}: {body[:2000]}"


def _call(action: str, object_type: str, fn: Any) -> Any:
    try:
        return fn()
    except httpx.HTTPStatusError as exc:
        logger.warning("%s on %s failed: HTTP %s", action, object_type, exc.response.status_code)
        raise ValueError(_describe_http_error(exc)) from exc


def netbox_create_object(object_type: str, data: dict) -> dict:
    """
    Create a new object in NetBox.

    Args:
        object_type: NetBox object type (e.g. "ipam.ipaddress", "dcim.device")
        data: Field values for the new object. Required fields depend on the
              type - consult the NetBox API docs or an existing object of the
              same type. Related objects are referenced by numeric id
              (e.g. {"device": 117}) or by their natural key where NetBox
              supports it.

    Returns:
        The created object as returned by NetBox, including its new "id".

    Raises:
        ValueError: object_type is unknown, or NetBox rejected the payload
                    (the message carries NetBox's own validation errors).

    Example:
        netbox_create_object("ipam.ipaddress",
                             {"address": "10.0.0.1/32", "status": "active",
                              "description": "example"})
    """
    endpoint = _resolve_endpoint(object_type)
    result = _call("create", object_type, lambda: _client().create(endpoint, data))
    logger.info("created %s id=%s", object_type, result.get("id"))
    return result


def netbox_update_object(object_type: str, object_id: int, data: dict) -> dict:
    """
    Update an existing NetBox object (partial update, HTTP PATCH).

    Only the fields present in `data` are changed; everything else on the
    object is left as-is. To clear a field, pass an explicit null/empty value.

    IMPORTANT: read the object first (netbox_get_object_by_id) when you intend
    to APPEND to a text field such as `description` - PATCH replaces the field
    value, it does not merge.

    Args:
        object_type: NetBox object type (e.g. "ipam.ipaddress")
        object_id: Numeric id of the object to update
        data: Fields to change

    Returns:
        The updated object as returned by NetBox.

    Raises:
        ValueError: object_type is unknown, or NetBox rejected the payload.

    Example:
        netbox_update_object("ipam.ipaddress", 1475,
                             {"description": "webhooks-gate egress"})
    """
    endpoint = _resolve_endpoint(object_type)
    result = _call("update", object_type, lambda: _client().update(endpoint, object_id, data))
    logger.info("updated %s id=%s", object_type, object_id)
    return result


def netbox_delete_object(object_type: str, object_id: int, confirm_display: str) -> dict:
    """
    Delete a NetBox object. Requires echoing the object's current display name.

    Deletion cannot be undone from the NetBox UI - the changelog records what
    was deleted, but restoring it is a manual re-create. To make a wrong id
    fail instead of destroying the wrong object, this tool re-reads the object
    and refuses unless `confirm_display` matches its current "display" value
    exactly.

    Args:
        object_type: NetBox object type (e.g. "ipam.ipaddress")
        object_id: Numeric id of the object to delete
        confirm_display: The object's current "display" string, as returned by
                         netbox_get_object_by_id. Read the object first.

    Returns:
        {"deleted": true, "object_type": ..., "object_id": ...,
         "object": <the object's state immediately before deletion>}

    Raises:
        ValueError: object_type is unknown, the object does not exist,
                    confirm_display does not match, or NetBox refused the
                    delete (e.g. another object still references it).

    Example:
        netbox_delete_object("ipam.ipaddress", 1475, "177.28.2.16/32")
    """
    endpoint = _resolve_endpoint(object_type)
    client = _client()

    current = _call("read-before-delete", object_type, lambda: client.get(endpoint, id=object_id))
    if not isinstance(current, dict) or not current:
        raise ValueError(f"{object_type} id={object_id} not found - nothing deleted")

    actual_display = str(current.get("display", ""))
    if confirm_display != actual_display:
        raise ValueError(
            f"confirm_display mismatch for {object_type} id={object_id}: "
            f"you passed {confirm_display!r}, the object is {actual_display!r}. "
            "Nothing was deleted. Re-read the object and pass its exact 'display' value."
        )

    _call("delete", object_type, lambda: client.delete(endpoint, object_id))
    logger.info("deleted %s id=%s (%s)", object_type, object_id, actual_display)
    return {
        "deleted": True,
        "object_type": object_type,
        "object_id": object_id,
        "object": current,
    }


WRITE_TOOLS = (netbox_create_object, netbox_update_object, netbox_delete_object)


def register(force: bool = False) -> list[str]:
    """Register the write tools on the upstream FastMCP instance.

    Returns the names of the tools registered (empty when writes are off).
    """
    # Printed rather than logged: registration happens before upstream's main()
    # configures logging, and "can this server write?" is the first thing you
    # want to see in the container log.
    if not (force or writes_enabled()):
        print(  # noqa: T201
            f"netbox-mcp-server: write tools DISABLED ({ENABLE_WRITES_ENV} not set), server is read-only",
            file=sys.stderr,
        )
        return []

    for tool in WRITE_TOOLS:
        upstream.mcp.tool(tool)

    names = [tool.__name__ for tool in WRITE_TOOLS]
    print(  # noqa: T201
        f"netbox-mcp-server: write tools ENABLED: {', '.join(names)}",
        file=sys.stderr,
    )
    return names
