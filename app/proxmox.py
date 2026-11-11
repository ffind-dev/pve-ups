"""Proxmox VE API client.

Shuts down a node via ``POST /nodes/{node}/status {command: shutdown}`` using an
API token (``user@realm!tokenid``) that only needs the ``Sys.PowerMgmt`` privilege.
Node shutdown lets PVE stop the guests in an orderly fashion according to their own
shutdown configuration, so we do not have to iterate guests ourselves.

Proxmox Backup Server speaks a very similar API but differs in the token header
scheme and in the privilege it checks — that lives in app/pbs.py; the engine reaches
both through app/targets.py and never imports either directly.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .config import HostConfig

log = logging.getLogger("pve-usv.proxmox")


@dataclass
class TestResult:
    """The shared result of a credential check — every target type returns this one."""

    ok: bool
    message: str
    has_power_mgmt: bool = False
    # Member of NODE_STATES: what the configured node name turned out to be. Its own
    # field rather than part of ``ok`` because the two answer different questions — the
    # credentials can be perfect while the name points at nothing.
    node_state: str = "unverified"
    # SSH targets only: the server's host key as "<algorithm> <base64>", so the UI can pin
    # it after the first successful test. Empty for every API target.
    host_key: str = ""


@dataclass
class NodeList:
    """Node names this API reported, for checking a configured node name against reality.

    ``GET /nodes`` is ``permissions => { user => 'all' }`` in PVE::API2::Nodes and returns
    every node; Sys.Audit only decides whether individual fields are redacted. So this is
    readable with the documented minimal token, which is what makes verification possible
    without asking anyone to widen their ACL.

    ``readable`` stays anyway, for the genuinely thin answers (network error, an API that
    surprises us). False means *no verdict* — never "the configured name is wrong".
    """

    readable: bool = False
    nodes: list[str] = field(default_factory=list)
    # The node behind THIS api_url, where the answer identifies it. /nodes does not mark
    # it; only /cluster/status does (see cluster.ClusterInfo.local_node), which is why
    # list_nodes() asks that one first.
    local: Optional[str] = None


# Closed set of node-name verdicts. The UI labels each via "nodest.<state>", so a new
# member needs a key in both dictionaries (tests/test_i18n.py enforces that).
NODE_STATES = (
    "ok",          # the name is one this API knows (and, where visible, the local one)
    "invalid",     # PVE's own schema would reject it — never even reaches the handler
    "wrong",       # syntactically fine, but no node of that name exists here
    "proxied",     # a real node, but not the one behind this api_url
    "unverified",  # nothing could be read; explicitly NOT a verdict
)

# Verbatim from PVE::JSONSchema::pve_verify_node_name (pve-common). A name failing this is
# rejected by the API's own parameter check with HTTP 400 — note there is no dot, so an
# FQDN never gets as far as the handler. Checked locally because it costs no round trip
# and gives the exact reason instead of a bare status code.
NODE_NAME_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?$")


@dataclass
class NodeVerdict:
    """What the configured node name turned out to be, and how to say it."""

    state: str = "unverified"   # member of NODE_STATES
    detail: str = ""            # one English sentence for the test message
    nodes: list[str] = field(default_factory=list)
    local: Optional[str] = None


# An unreachable address must not eat the whole budget waiting for a TCP handshake:
# during an outage the remaining time belongs to the other targets.
CONNECT_TIMEOUT_S = 10.0


def _auth_header(host: HostConfig) -> dict[str, str]:
    secret = host.token_secret.get_secret_value()
    return {"Authorization": f"PVEAPIToken={host.token_id}={secret}"}


def _client(host: HostConfig, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=host.api_url.rstrip("/") + "/api2/json",
        headers=_auth_header(host),
        verify=host.verify_tls,
        timeout=httpx.Timeout(timeout, connect=min(timeout, CONNECT_TIMEOUT_S)),
    )


async def verify_node(
    host: HostConfig, known: Optional[NodeList] = None, timeout: float = 10.0
) -> NodeVerdict:
    """Check the configured node name against the names this API reports. Never raises.

    Ordered so the cheapest and most certain answer wins, and so the *evidence* always
    beats a string comparison: where PVE itself lists the name, nothing else gets a say.
    That is also what keeps us out of the question whether the path segment is matched
    case-sensitively — if the name is in the list, it works, whatever the rule is.

    ``known`` lets a caller hand in a listing it already has (a cluster member's
    /cluster/status names every node and marks the local one), which saves the round trip.
    Marking the local node is what makes the ``proxied`` verdict possible at all — ``GET
    /nodes`` names the members without saying which one answered, so on a cluster every
    other member's name passed as "ok". list_nodes() therefore tries /cluster/status
    itself; without Sys.Audit it falls back to the index and the verdict stops at "the
    name exists here", which is honest rather than wrong.
    """
    name = (host.api_node or "").strip()
    if not NODE_NAME_RE.match(name):
        return NodeVerdict(
            state="invalid",
            detail=(
                f"'{name}' is not a valid Proxmox node name — the API rejects it in its "
                f"own parameter check (HTTP 400). Only letters, digits and '-' are "
                f"allowed, so a domain suffix or an IP address never works."
            ),
        )

    if known is None or not known.readable:
        known = await list_nodes(host, timeout)
    if not known.readable:
        return NodeVerdict(
            state="unverified",
            detail="The node name could not be verified: this API named no nodes.",
        )

    verdict = NodeVerdict(nodes=list(known.nodes), local=known.local)
    if name not in known.nodes:
        verdict.state = "wrong"
        verdict.detail = (
            f"'{name}' is not one of the nodes this API knows "
            f"({', '.join(known.nodes)})."
        )
        return verdict
    if known.local and name != known.local:
        # A real node, reached by proxy through a different machine's API. Harmless while
        # this entry owns its API URL — the shutdown addresses the node behind it directly
        # — but where entries share one URL the name IS the path, and then this is the one
        # failure mode no name list can see: the wrong host goes down, this one stays up.
        verdict.state = "proxied"
        verdict.detail = (
            f"'{name}' is a real node, but this API URL belongs to '{known.local}'. As "
            f"long as this entry has that URL to itself the shutdown still reaches "
            f"'{known.local}'; share the URL with another entry and it would be proxied "
            f"to '{name}' instead, leaving '{known.local}' running."
        )
        return verdict
    verdict.state = "ok"
    return verdict


async def test_connection(host: HostConfig, timeout: float = 10.0) -> TestResult:
    """Validate URL, token, the power-management privilege and the node name."""
    # A SLICE of the caller's budget, not the whole of it, and for each of the two
    # requests below. targets.test_connection() cuts the call off at timeout +
    # DEADLINE_GRACE_S, so at the full timeout each these two alone could reach it —
    # before verify_node() had even started. A host that is merely slow (a loaded
    # pveproxy, a WAN link) then came back as "gave up", which the self-test writes up as
    # a CRITICAL failure for a target whose credentials are perfectly fine. Same
    # arithmetic list_nodes() already does with per_call.
    per_call = max(2.0, timeout / 3)
    try:
        async with _client(host, per_call) as client:
            # Version endpoint confirms reachability + token validity.
            resp = await client.get("/version")
            if resp.status_code == 401:
                return TestResult(False, "Authentication failed (token invalid?)")
            resp.raise_for_status()

            # Check effective permissions for Sys.PowerMgmt on this node.
            perm = await client.get("/access/permissions")
            has_power = False
            if perm.status_code == 200:
                data = perm.json().get("data", {})
                for path in (f"/nodes/{host.name}", "/nodes", "/"):
                    if data.get(path, {}).get("Sys.PowerMgmt"):
                        has_power = True
                        break

            if not has_power:
                return TestResult(
                    True,
                    "Connection ok, but the 'Sys.PowerMgmt' privilege could not be "
                    "confirmed for this node. A shutdown might be rejected.",
                    has_power_mgmt=False,
                )
    except httpx.HTTPStatusError as exc:
        return TestResult(False, f"HTTP {exc.response.status_code}: {exc.response.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        return TestResult(False, f"Connection error: {exc}")

    # Only once the credentials work: an unreachable API cannot answer this either, and it
    # would add its own timeout to the wait. Done HERE rather than in the web layer so the
    # scheduled self-test inherits it through targets.test_connection() — the node name is
    # the one setting nothing else validates, and it decides whether a shutdown lands.
    # The last slice, sized like the two above: with /version and /access/permissions
    # already done, three full timeouts would overrun the deadline in targets.py and turn
    # a slow-but-alive host into "gave up" instead of a real verdict.
    verdict = await verify_node(host, timeout=min(per_call, 5.0))
    message = "Connection and 'Sys.PowerMgmt' privilege ok."
    if verdict.state != "ok":
        message = f"{message} {verdict.detail}"
    return TestResult(True, message, has_power_mgmt=True, node_state=verdict.state)


# The node segment PVE handles without consulting the cluster. PVE::HTTPServer's
# rest_handler proxies only when `$node ne 'localhost' && $node ne nodename()`, so this
# form never reaches remote_node_ip() — no pmxcfs, no corosync, no quorum. During an
# outage, when nodes are dropping one by one, that is exactly the dependency the shutdown
# path should not have. It is also what app/pbs.py has always used, for the same reason.
LOCAL_NODE = "localhost"


async def shutdown_node(
    host: HostConfig, timeout: float = 60.0, use_localhost: bool = False
) -> tuple[bool, str]:
    """Issue an orderly node shutdown. Returns (ok, message).

    ``use_localhost`` addresses the node behind this API URL directly instead of by its
    configured name. The caller decides it (the engine, from AppConfig.api_url_is_unique):
    with one URL per node — what the manual has always required, because a node already
    powered off cannot proxy for the ones still to come — the machine behind the URL *is*
    the intended one, and a name that can be misspelled adds nothing but a way to fail.

    Whichever form goes first, a failure is retried once with the other. That covers both
    directions without having to know in advance which applies: a misspelled name answers
    400 (schema) or 500 (unresolvable), while a token holding Sys.PowerMgmt on
    /nodes/<name> rather than /nodes answers 403 for the localhost form. 403 is
    deliberately retried too — a refused call did nothing, so a second one costs only a
    round trip in a shutdown that has already failed.
    """
    first = LOCAL_NODE if use_localhost else host.api_node
    second = host.api_node if use_localhost else LOCAL_NODE
    attempts = [first] if first == second else [first, second]
    errors = []
    try:
        # The budget is shared between the attempts, not handed to each of them.
        # targets.shutdown() cuts the whole call off at timeout + DEADLINE_GRACE_S, so a
        # first form that HANGS rather than answering (connection accepted, then silence)
        # used to leave the second one five seconds — and the retry that this whole
        # routine exists for never happened, reporting a bare "gave up" instead. The
        # normal failures it covers (400/500 for a misspelled name, 403 for a
        # node-scoped privilege) answer at once and are unaffected either way.
        async with _client(host, timeout / len(attempts)) as client:
            for node in attempts:
                resp = await client.post(
                    f"/nodes/{node}/status", data={"command": "shutdown"}
                )
                if resp.status_code in (200, 201):
                    if node == first:
                        return True, f"Shutdown command accepted (/nodes/{node})"
                    # Worth shouting about: the machine is down, but the configuration
                    # that should have worked did not, and it will not fix itself.
                    return True, (
                        f"Shutdown command accepted via /nodes/{node}, after "
                        f"/nodes/{first} failed ({errors[0]}). Fix this host entry."
                    )
                errors.append(f"HTTP {resp.status_code}: {resp.text[:200]}")
    except Exception as exc:  # noqa: BLE001
        log.error("Shutdown of %s failed: %s", host.name, exc)
        return False, f"Error: {exc}"
    return False, "; ".join(f"/nodes/{n}: {e}" for n, e in zip(attempts, errors))


async def list_nodes(host: HostConfig, timeout: float = 10.0) -> NodeList:
    """Read the nodes this API knows, to check a configured node name against reality.

    Two sources, best first: ``GET /cluster/status`` names every member **and** marks the
    one that answered (``local: 1``), which is the only way to tell "a node of this
    cluster" from "the node behind this API URL". It needs Sys.Audit on '/', so it is not
    always readable; ``GET /nodes`` then answers with the plain index, which every valid
    token may read (``permissions => { user => 'all' }``).

    Never raises, and never concludes anything from a thin answer: the node name ends up
    verbatim in ``POST /nodes/{node}/status``, so a wrong one fails only during a real
    outage — but a token that may not enumerate nodes is not evidence of a wrong name.
    Hence anything other than a non-empty list leaves ``readable`` False.
    """
    # Both calls share the caller's budget: test_connection() hands this one a slice of
    # its own, and two full timeouts in a row would break the deadline in targets.py and
    # turn a slow host into "gave up" instead of a verdict.
    per_call = max(2.0, timeout / 2)
    known = await _read_node_source(host, "/cluster/status", per_call)
    if known.readable:
        return known
    return await _read_node_source(host, "/nodes", per_call)


async def _read_node_source(host: HostConfig, path: str, timeout: float) -> NodeList:
    """One node listing endpoint -> NodeList. Never raises; thin answers stay unreadable."""
    try:
        async with _client(host, timeout) as client:
            resp = await client.get(path)
            if resp.status_code >= 400:
                return NodeList()
            data = resp.json().get("data")
    except Exception as exc:  # noqa: BLE001 - a read must never break the caller
        log.debug("%s of %s could not be read: %s", path, host.name, exc)
        return NodeList()
    if not isinstance(data, list):
        return NodeList()
    names = []
    local = None
    for entry in data:
        if not isinstance(entry, dict):
            continue
        # The node index keys this "node"; /cluster/status keys the same thing "name" and
        # carries entries that are not nodes at all (the "cluster" record). Accepting both
        # keys keeps one parser for two shapes.
        if entry.get("type") not in (None, "node"):
            continue
        name = str(entry.get("node") or entry.get("name") or "")
        if not name:
            continue
        names.append(name)
        if entry.get("local"):
            local = name
    return NodeList(readable=bool(names), nodes=names, local=local)
