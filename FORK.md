# About this fork

Fork of [netboxlabs/netbox-mcp-server](https://github.com/netboxlabs/netbox-mcp-server),
tracked as the `upstream` git remote. Upstream releases arrive as ordinary merges.

## Why it exists

Upstream is read-only **by design** — its contributing guide rules write tools out
of scope. We want an agent to be able to write back to NetBox: the recurring case
is annotating the IPAM object it just reasoned about, which otherwise means
hand-rolled `curl` against the NetBox API.

Upstream's `NetBoxRestClient` already implements `create` / `update` / `delete`;
only the MCP tool layer omits them. The fork's entire delta is a small package
that registers three more tools on the same FastMCP instance.

## The invariant that keeps upstream merges free

**Fork code never modifies an upstream file.** Everything added lives in paths
upstream does not use:

| Path | What |
|---|---|
| `src/netbox_mcp_server/writes/` | the write tools and their entry point |
| `tests/test_write_tools.py` | their tests |
| `FORK.md` | this file |
| `.github/workflows/fork-*.yml` | fork automation |

No hunks in `server.py`, `pyproject.toml`, `Dockerfile` or anywhere else, so
`git merge upstream/<tag>` reduces to fast-forwarding upstream's own files. A
conflict outside those paths means the invariant was broken.

The write tools are activated by the **deployment**, not by the image: run
`python -m netbox_mcp_server.writes` instead of the upstream `netbox-mcp-server`
console script. Run the upstream entry point and the same image gives you the
stock read-only server.

**One exception: `uv.lock`.** It is refreshed here to clear CVEs in transitive
dependencies, because upstream's release cadence is not a security cadence. Its
conflicts have a deterministic resolution — take upstream's copy, regenerate:

```bash
git checkout --theirs uv.lock && uv lock --upgrade && git add uv.lock
```

`fork-upstream-sync.yml` does exactly that by itself when `uv.lock` is the *only*
conflicted path, and fails for anything else.

Hold `ruff` at the version upstream's lock pins (`--upgrade-package ruff==<theirs>`).
Upstream's CI runs `ruff format --check .` across the repo including its own
markdown, and a newer ruff reformats python blocks inside `CLAUDE.md` — bumping
the linter turns upstream's docs red in a PR that has nothing to do with them.
The image builds with `uv sync --no-dev`, so dev-group versions never ship.

Two more things a future reader will want to un-do, so they are stated here:
upstream's `CLAUDE.md` forbids write operations and `print` statements. Both are
deliberate divergences — the write tools are the point of the fork, and
`write_tools.py` prints its enabled/disabled banner to stderr because
registration happens before upstream's `main()` configures logging, the same
exception upstream makes for its own pre-logging config errors.

## Merging upstream

Automated: `.github/workflows/fork-upstream-sync.yml` runs weekly (and on demand),
finds the newest upstream release tag and opens a PR merging it, refreshing the
lock in the same PR. A conflict fails the run instead of pushing a half-merged
branch.

By hand:

```bash
git remote add upstream https://github.com/netboxlabs/netbox-mcp-server.git   # once
git fetch upstream --tags
git checkout -b chore/upstream-v1.3.0 main
git merge v1.3.0            # expected: clean, only upstream files change
uv sync --all-groups && uv run ruff check . && uv run ruff format --check . && uv run pytest
```

Release tags are `<upstream-version>-writes.<n>`, e.g. `v1.2.1-writes.1`.

## The tools

Off unless `NETBOX_MCP_ENABLE_WRITES` is `1`/`true`/`yes`/`on` — when off they are
not registered at all, so a client cannot see them.

| Tool | Method | Guard |
|---|---|---|
| `netbox_create_object(object_type, data)` | POST | object type must be a known NetBox type |
| `netbox_update_object(object_type, object_id, data)` | **PATCH** | partial update: unmentioned fields are untouched |
| `netbox_delete_object(object_type, object_id, confirm_display)` | GET then DELETE | refuses unless `confirm_display` equals the object's live `display` |

All three return the resulting object (delete returns the pre-delete state) and
surface NetBox's own validation errors rather than a bare status code.

NetBox permissions are the real enforcement layer: the token's user needs
`add`/`change`/`delete` object permissions or every write is a 403, whatever the
token's write flag says.
