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
| `src/netbox_mcp_server/writes/` | the write tools, Cloudflare Access mode (`access.py`), the optional agent `reason` (`reason.py`) and their entry point |
| `tests/test_write_tools.py`, `tests/test_access.py`, `tests/test_reason.py`, `tests/conftest.py` | their tests |
| `FORK.md` | this file |
| `.github/workflows/fork-*.yml` | fork automation |

No hunks in `server.py` or anywhere else (except the two exceptions below), so
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

**Second exception: packages only through nora** (issues-maestra#1649). Every
Python package comes from `packages.maestra.io`, so the fork carries small hunks
in upstream files:

| File | Hunk |
|---|---|
| `pyproject.toml` | the `[[tool.uv.index]]` block `maestra` (`default = true`) |
| `uv.lock` | resolved through that index, so every URL is nora's |
| `Dockerfile` | uv bind-mounted from `packages.maestra.io/ghcr/astral-sh/uv` instead of `pip install uv`; both `uv sync` steps read the BuildKit secret `PRIVATE_PACKAGES_TOKEN`; the `FROM` images are `packages.maestra.io/docker-hub/library/python` (same tag and digest) and the runtime `apk` runs through `nora-apk` |
| `.github/workflows/test.yml`, `container-scan.yaml` | `maestra-io/github-actions/uv-nora` (OIDC token for nora), `uv sync --locked`; `container-scan` also has the nora docker credential helper for the base images, and scans with `maestra-io/github-actions/trivy-nora` instead of `aquasecurity/trivy-action` (same inputs; trivy and its DBs through nora) |
| `.github/workflows/docker-publish.yml` | both `docker/setup-buildx-action` steps are `maestra-io/github-actions/setup-buildx` (BuildKit image from nora), `id-token: write` on `build`; an upstream bump of the setup-buildx pin conflicts there and resolves by keeping ours |

They sit away from the lines upstream releases touch (version), except the two
`FROM` lines: an upstream digest bump conflicts there and resolves by taking
upstream's digest under the `packages.maestra.io/docker-hub/library/` prefix;
the `uv.lock` conflict resolves as below, and the regenerated lock comes from
nora because the merged `pyproject.toml` keeps the index. Anything that builds the image (`maestra-io/netbox`) passes the secret.

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

## Release tags

Release tags are `v<upstream-version>-writes.<n>`, e.g. `v1.2.1-writes.1`.

`.github/workflows/fork-autotag.yml` runs on every push to `main` and mints one
when it is needed, so merging the sync PR is the whole release. It derives
`<upstream-version>` from `__version__` and increments `<n>` only when something
under `src/`, `pyproject.toml`, `uv.lock` or `Dockerfile` changed since the
previous tag — a docs or CI push runs the workflow but does not burn a version.
(It can also be re-run by hand, but only on `main`: a `workflow_dispatch` aimed
at any other ref is skipped, so a tag can never name an unmerged commit.)

The tag is the contract with the image build: `maestra-io/netbox`
`.github/workflows/netbox-mcp-server-image.yml` checks this fork out at that ref,
builds it and pushes it to ECR. Renovate watches this repo's tags and opens the
bump there, so nothing between a merged upstream sync and an image in ECR is a
human step. (What runs in the cluster is still a separate hop — the Archestra
platform owns those Deployments and no IaC pins their image.)

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

## The agent `reason`

`python -m netbox_mcp_server.writes` adds an optional `reason` to every tool,
read and write (`writes/reason.py`, issues-maestra#1768): the agent's context for
the call. It is never required and never rejects a call, whatever its value
(absent, empty, wrong type, overlong); a FastMCP middleware declares it in each
input schema and removes it before FastMCP validates the arguments (FastMCP
rejects an undeclared argument). Each call writes one JSON line to stderr:
`{"msg": "tool call", "server": "netbox", "tool", "outcome", "latency_ms",
"user", "user_source", "reason", "reason_present", "cf_ray"}`, with the reason
clipped to 300 characters and `user` the verified Access email (Access mode
only). No other argument and no result is logged. In Access mode a write puts
the reason next to the email in its changelog message (below).

## Cloudflare Access mode

For the HTTP transport behind a Cloudflare tunnel and an Access application
(`netbox-mcp.maestrateam.io`, issues-maestra#1768). Off unless the variables
below are set, so the stdio deployment is unchanged. Same env names and rules
as maestra-io/kargo-mcp `src/access.ts`.

| Variable | Value |
|---|---|
| `CF_ACCESS_AUD` | Application Audience (AUD) tag of the Access app for the host |
| `CF_ACCESS_TEAM_DOMAIN` | bare host `<team>.cloudflareaccess.com` (ours: `maestra-io.cloudflareaccess.com`) |

Either one defined without a usable pair, Access with `TRANSPORT=stdio`, or
Access next to `MCP_AUTH_TOKEN` makes `python -m netbox_mcp_server.writes` exit 1
instead of starting open.

When on, every request to `/mcp` needs `Cf-Access-Jwt-Assertion`, verified by
FastMCP's `JWTVerifier`: RS256 against `https://<team>/cdn-cgi/access/certs`
(cached 1 h, refetched on an unknown `kid`, so a key rotation lands at once; a
failed fetch refuses the request and logs a warning), `iss` = `https://<team>`,
`aud` = `CF_ACCESS_AUD`, `exp` required, `nbf` with 30 s of skew. The token is
read from that header only: a client `Authorization` header is dropped, a
repeated Access header counts as none. Anything else is a `401`.

| Request | Answer |
|---|---|
| no token, forged, expired, foreign audience, repeated header | `401`, nothing reaches NetBox |
| valid token with `email` | read and write tools run |
| valid service token (no `email`) | read tools run; **write tools refuse** before any NetBox call |

NetBox is still called with the shared service token (`NETBOX_TOKEN`). Each
write carries `changelog_message: "via netbox-mcp by <email>[ (reason: <the
agent's reason>)][: <caller's message>]"` (NetBox 4.4+, max 200 characters, the
email first so a caller's message cannot hide it), so the NetBox changelog shows the person next to the
service user. Delete sends it in the `DELETE` body. Outside Access mode no
message is added and the calls are byte-for-byte upstream's.

