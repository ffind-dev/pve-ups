"""Optional notifications: any number of webhooks.

Notifications are best-effort: a failure to notify must never affect the shutdown
logic, so every send is wrapped and only logged on error. The webhooks are sent
concurrently and reported one by one, so a target that accepts the connection and then
goes quiet cannot delay its peers — or the poll loop this runs on.

The payload shape is selectable (see config.WebhookFormat). Every format is one entry in
FORMATTERS, rendering a notification into keyword arguments for ``httpx.post`` — a new
target system is a table row, not new plumbing. Message texts are English only.

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import Callable

import httpx

from . import __version__, db
from .config import Notifications, WebhookConfig, WebhookFormat

log = logging.getLogger("pve-usv.notify")

# Total budget for one round of notifications, across all targets. The per-target
# httpx timeout is 10 s, so this sits just above it: that one should be what normally
# fires, because its message names the actual failure. Same doctrine (and the same
# reason) as targets.DEADLINE_GRACE_S and sources.POLL_GRACE_S — this is the backstop
# for what httpx does not bound, and a read timeout bounds one read, not a server that
# dribbles a byte at a time.
#
# It matters because notify() is awaited from Engine._emit(), which is awaited from the
# poll loop: from _evaluate_ups() before the hosts are even evaluated, from
# _prepare_clusters() before the cluster preparation starts, and from _fire_host()
# inside a shutdown stage — where the next stage, the appliance's own host, waits on it.
# "Best effort" has to mean bounded, or a dead chat server spends the battery.
NOTIFY_BUDGET_S = 12.0

# Severity ranking for the "send from this level upwards" filter.
_RANK = {db.INFO: 0, db.WARNING: 1, db.CRITICAL: 2}

# Adaptive card colours per severity (Teams renders these as green/amber/red).
_CARD_COLOR = {db.INFO: "Good", db.WARNING: "Warning", db.CRITICAL: "Attention"}

# The same three states in the notations the other targets want.
_SLACK_COLOR = {db.INFO: "#2eb886", db.WARNING: "#daa038", db.CRITICAL: "#d40e0d"}
_DISCORD_COLOR = {db.INFO: 0x2EB886, db.WARNING: 0xDAA038, db.CRITICAL: 0xD40E0D}
_NTFY_PRIORITY = {db.INFO: "default", db.WARNING: "high", db.CRITICAL: "urgent"}
_NTFY_TAGS = {db.INFO: "information_source", db.WARNING: "warning", db.CRITICAL: "rotating_light"}


def _value(setting) -> str:
    """Enum member or bare string — plain attribute assignment skips pydantic validation."""
    return getattr(setting, "value", setting)


def _rank(severity: str) -> int:
    """Rank of a severity; an unknown one counts as a warning so it is never dropped."""
    return _RANK.get(severity, _RANK[db.WARNING])


def _passes(hook: WebhookConfig, severity: str, hint: bool = False) -> bool:
    # A configuration hint (#50) is held back from a target that opted out of them; the
    # caller has already made sure a CRITICAL is never one (Engine._emit).
    if hint and not hook.config_hints:
        return False
    return _rank(severity) >= _rank(_value(hook.min_severity))


# --- payload rendering ------------------------------------------------------
def _facts(payload: dict) -> list[tuple[str, str]]:
    """Compact key/value summary of the status snapshot, shared by teams and text.

    Reads defensively throughout: the snapshot is the engine's business, and a changed
    key must degrade the notification, never break it.
    """
    facts: list[tuple[str, str]] = []
    try:
        ups_lines = []
        for u in payload.get("ups") or []:
            bits = []
            if not u.get("reachable"):
                bits.append("unreachable")
            else:
                bits.append(str(u.get("power_source") or "?"))
                if u.get("battery_charge_pct") is not None:
                    bits.append(f"{u['battery_charge_pct']} %")
                if u.get("runtime_remaining_min") is not None:
                    bits.append(f"{u['runtime_remaining_min']} min")
            ups_lines.append(f"{u.get('name') or u.get('id') or 'UPS'}: {', '.join(bits)}")
        if ups_lines:
            facts.append(("UPS", " · ".join(ups_lines)))

        host_lines = []
        for h in payload.get("hosts") or []:
            state = h.get("shutdown_state") or ("eligible" if h.get("eligible") else "waiting")
            host_lines.append(f"{h.get('name') or '?'}: {state}")
        if host_lines:
            facts.append(("Hosts", " · ".join(host_lines)))

        app = payload.get("appliance") or {}
        facts.append(("Mode", "DRY-RUN" if app.get("dry_run") else "ARMED"))
        facts.append(("Time", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")))
        facts.append(("Appliance", f"PVE-UPS {app.get('version') or __version__}"))
    except Exception as exc:  # noqa: BLE001 - a malformed snapshot must not stop the send
        log.warning("Could not summarise the status snapshot: %s", exc)
    return facts


def _render_json(subject: str, body: str, severity: str, payload: dict) -> dict:
    """The original payload: subject, body and the complete status snapshot."""
    return {"json": {"subject": subject, "body": body, "severity": severity, "status": payload}}


def _render_teams(subject: str, body: str, severity: str, payload: dict) -> dict:
    """Microsoft Teams adaptive card, wrapped in the incoming-webhook message envelope."""
    card_body: list[dict] = [
        {
            "type": "TextBlock",
            "text": subject,
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
            "color": _CARD_COLOR.get(severity, "Default"),
        }
    ]
    if body:
        card_body.append({"type": "TextBlock", "text": body, "wrap": True})
    facts = _facts(payload)
    if facts:
        card_body.append(
            {"type": "FactSet", "facts": [{"title": k, "value": v} for k, v in facts]}
        )
    return {
        "json": {
            "type": "message",
            "attachments": [
                {
                    "contentType": "application/vnd.microsoft.card.adaptive",
                    "contentUrl": None,
                    "content": {
                        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                        "type": "AdaptiveCard",
                        "version": "1.4",
                        "body": card_body,
                    },
                }
            ],
        }
    }


def _plain_lines(subject: str, body: str, severity: str, payload: dict) -> list[str]:
    """The plain-text rendering, shared by the text and ntfy formats."""
    lines = [f"[{severity.upper()}] {subject}"]
    if body:
        lines.append(body)
    lines.extend(f"{k}: {v}" for k, v in _facts(payload))
    return lines


def _render_text(subject: str, body: str, severity: str, payload: dict) -> dict:
    """Human-readable short status as text/plain."""
    return {
        "content": "\n".join(_plain_lines(subject, body, severity, payload)).encode("utf-8"),
        "headers": {"Content-Type": "text/plain; charset=utf-8"},
    }


def _render_slack(subject: str, body: str, severity: str, payload: dict) -> dict:
    """Slack incoming webhook: an attachment, so the severity shows as a colour bar.

    ``text`` stays filled as the notification/fallback line — Slack uses it for the push
    notification and for clients that do not render attachments.
    """
    fields = [
        {"title": k, "value": v, "short": False} for k, v in _facts(payload)
    ]
    attachment: dict = {"color": _SLACK_COLOR.get(severity, "#6b7890"), "fallback": subject}
    if fields:
        attachment["fields"] = fields
    attachment["title"] = subject
    if body:
        attachment["text"] = body
    return {"json": {"text": f"[{severity.upper()}] {subject}", "attachments": [attachment]}}


def _render_discord(subject: str, body: str, severity: str, payload: dict) -> dict:
    """Discord webhook: an embed with the facts as fields, colour by severity."""
    embed: dict = {"title": subject[:256], "color": _DISCORD_COLOR.get(severity, 0x6B7890)}
    if body:
        embed["description"] = body[:4096]
    fields = [{"name": k, "value": v, "inline": False} for k, v in _facts(payload)]
    if fields:
        embed["fields"] = fields[:25]  # Discord rejects an embed with more than 25 fields
    return {"json": {"content": f"[{severity.upper()}] {subject}"[:2000], "embeds": [embed]}}


def _render_ntfy(subject: str, body: str, severity: str, payload: dict) -> dict:
    """ntfy: the plain body, with the metadata ntfy expects in headers.

    Title/Priority/Tags are how ntfy renders a message; the body itself is plain text.
    """
    lines = _plain_lines(subject, body, severity, payload)[1:]  # subject goes in the Title
    return {
        "content": "\n".join(lines).encode("utf-8"),
        "headers": {
            "Content-Type": "text/plain; charset=utf-8",
            # Header values must be latin-1 encodable; ntfy decodes RFC 2047, but keeping
            # the title ASCII-safe avoids depending on that.
            "Title": subject.encode("ascii", "replace").decode("ascii"),
            "Priority": _NTFY_PRIORITY.get(severity, "default"),
            "Tags": _NTFY_TAGS.get(severity, "bell"),
        },
    }


def _json_escape(value: str) -> str:
    """Escape a value for embedding inside a JSON string literal (without the quotes)."""
    return json.dumps(str(value))[1:-1]


def _placeholders(subject: str, body: str, severity: str, payload: dict) -> dict[str, str]:
    """The substitution table for the ``custom`` format."""
    facts = _facts(payload)
    return {
        "subject": subject,
        "body": body,
        "severity": severity,
        "severity_upper": severity.upper(),
        "facts": "\n".join(f"{k}: {v}" for k, v in facts),
        "facts_json": json.dumps(dict(facts)),
        "status_json": json.dumps(payload, default=str),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": str(payload.get("appliance", {}).get("version") or __version__),
    }


def render_custom(template: str, content_type: str, subject: str, body: str,
                  severity: str, payload: dict) -> dict:
    """Substitute {{placeholders}} in a user-supplied body. NOT an expression language.

    Deliberately plain replacement: an actual template engine on the notification path
    would be new attack surface (SSTI) for no gain — the placeholders below cover the
    target systems. Values are JSON-escaped when the body is JSON, so a quote in an event
    text cannot produce a malformed payload.
    """
    ctype = content_type or "application/json"
    is_json = "json" in ctype.lower()
    values = _placeholders(subject, body, severity, payload)
    out = template
    for key, value in values.items():
        # The pre-rendered JSON blobs are already valid JSON and must not be escaped again.
        raw = key in ("facts_json", "status_json")
        replacement = value if (raw or not is_json) else _json_escape(value)
        out = out.replace("{{" + key + "}}", replacement)
    return {"content": out.encode("utf-8"), "headers": {"Content-Type": ctype}}


def _render_custom(subject: str, body: str, severity: str, payload: dict) -> dict:
    """Table entry for the custom format; the real work needs the hook's own settings,
    so send_webhook() calls render_custom() directly. This is the safe fallback when a
    hook says "custom" but has no template."""
    return _render_text(subject, body, severity, payload)


# The extension point: one entry per selectable format (see config.WebhookFormat).
FORMATTERS: dict[str, Callable[[str, str, str, dict], dict]] = {
    WebhookFormat.json.value: _render_json,
    WebhookFormat.teams.value: _render_teams,
    WebhookFormat.text.value: _render_text,
    WebhookFormat.slack.value: _render_slack,
    WebhookFormat.discord.value: _render_discord,
    WebhookFormat.ntfy.value: _render_ntfy,
    WebhookFormat.custom.value: _render_custom,
}


# --- sending ----------------------------------------------------------------
async def send_webhook(
    hook: WebhookConfig, subject: str, body: str, severity: str, payload: dict
) -> str:
    """POST one notification and return a short result. Raises on any failure.

    Deliberately ignores ``enabled`` and ``min_severity``: the filter belongs to the
    event path in :func:`notify`, so the UI's test button can send unconditionally.
    """
    fmt = _value(hook.format)
    if fmt == WebhookFormat.custom.value and (hook.template or "").strip():
        kwargs = render_custom(
            hook.template, hook.content_type, subject, body, severity, payload
        )
    else:
        kwargs = FORMATTERS.get(fmt, _render_json)(subject, body, severity, payload)

    # Headers ride along with whatever the formatter produced, in a fixed precedence:
    # formatter -> extra_headers -> auth header.
    #
    # extra_headers beats the formatter on purpose — a target that wants a different
    # Content-Type than the payload shape implies has no other way to say so. The auth
    # header is applied last and therefore always wins: it is the only one of the three
    # carrying a secret, and a plain-text entry in extra_headers must not be able to
    # shadow it with an empty or stale value.
    headers = {**kwargs.get("headers", {}), **(hook.extra_headers or {})}
    name = (hook.auth_header_name or "").strip()
    if name:
        value = hook.auth_header_value.get_secret_value()
        if value:
            headers[name] = value
    if headers:
        kwargs["headers"] = headers

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(hook.url, **kwargs)
    resp.raise_for_status()
    return f"HTTP {resp.status_code}"


# Last delivery per webhook id. Module level because notify() is a free function on the
# engine's poll loop and there is one process with one configuration; engine.snapshot()
# reads it so the UI can show a webhook that stopped working.
DELIVERY: dict[str, dict] = {}


def delivery_state() -> dict[str, dict]:
    """Copy of the last delivery outcome per webhook id, for /api/status."""
    return {k: dict(v) for k, v in DELIVERY.items()}


def forget_deliveries(keep: set[str]) -> None:
    """Drop the delivery record of every webhook id not in ``keep``.

    Called when a new configuration arrives. The record is keyed by the webhook id, and
    an id can come back: assign_webhook_ids() hands out "webhookN" for an entry that has
    none, so a deleted target's failure would be inherited by an unrelated new one and
    shown on its card as its own.
    """
    for key in list(DELIVERY):
        if key not in keep:
            del DELIVERY[key]


# Anything that looks like a URL, so a stray one in an exception text cannot travel.
_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+")


def safe_error(result) -> str:
    """One short, URL-free description of a failed send.

    This exists because of where the string ends up. ``/api/status`` is deliberately
    public and secret-free, and the webhook block is read straight out of it — while a
    webhook URL IS the credential for Slack, Discord, Teams and ntfy alike. httpx spells
    its status errors as "Client error '401 Unauthorized' for url '<the whole thing>'",
    so storing ``str(exc)`` published the secret to anyone who could reach the appliance.
    The second path is worse: the snapshot travels on as the ``status`` field of every
    notification, so target A's URL would be POSTed to target B.

    The full text still reaches the journal and the event log, neither of which is public.
    """
    if isinstance(result, httpx.HTTPStatusError):
        # The status code is the whole diagnosis here (401 = token, 404 = deleted
        # connector, 429 = rate limit) and it carries nothing secret.
        return f"HTTP {result.response.status_code} {result.response.reason_phrase}".strip()
    if isinstance(result, (asyncio.TimeoutError, TimeoutError)):
        return f"No answer within {NOTIFY_BUDGET_S:.0f}s"
    if isinstance(result, httpx.HTTPError):
        # Transport failures (DNS, TLS, connect): the class name says which, and httpx
        # puts the host in the message often enough that the text is not worth the risk.
        return type(result).__name__
    # Anything unforeseen: keep the text, but never a URL inside it.
    return _URL_RE.sub("<url>", str(result) or type(result).__name__)


def _record_delivery(hook: WebhookConfig, result) -> None:
    """Remember how one send went, and say so in the product the first time it fails.

    Until this existed, a failed notification reached exactly one place: a log.warning in
    journald. Not the event log, not /api/health, not the UI. A webhook whose token had
    expired therefore stopped working in complete silence, and the outage it was supposed
    to announce was the moment you found out — while the *shutdown* credentials next door
    get a scheduled self-test, a dashboard chip and an event of their own.

    Reported once per run of failures, not once per event: during an outage the engine
    emits steadily, and a dead target would otherwise bury the log it is competing with.
    """
    failed = isinstance(result, BaseException)
    was_ok = DELIVERY.get(hook.id, {}).get("ok")
    DELIVERY[hook.id] = {
        "ok": not failed,
        "at": datetime.now(timezone.utc).isoformat(),
        # Sanitised, because this one is read out of the public /api/status — see
        # safe_error(). The full text goes to the journal and the event log below.
        "error": safe_error(result) if failed else None,
    }
    if not failed:
        return
    # The journal is the one place the untouched text may go: it is root-readable on the
    # appliance and reaches no API at all.
    log.warning("Webhook '%s' failed: %s", hook.label, result)
    if was_ok is False:
        return  # already reported for this run of failures
    try:
        # db.log_event directly, never through the engine's _emit: that one calls
        # notify(), and a failing webhook would then notify about itself for ever.
        #
        # Sanitised as well, and not by oversight: /api/status ships the last 48 h of the
        # event log alongside the snapshot, so an event body is exactly as public as the
        # delivery state above.
        db.log_event(
            f"Webhook '{hook.label}' failed",
            f"{safe_error(result)}. Notifications to this target are not arriving — check "
            f"the URL, the format and any auth header under Settings -> Notifications.",
            db.WARNING,
        )
    except Exception as exc:  # noqa: BLE001 - the event log must never break a send
        log.warning("Event log write failed: %s", exc)


async def notify(
    notifications: Notifications,
    subject: str,
    body: str,
    payload: dict,
    severity: str = db.INFO,
    hint: bool = False,
) -> None:
    """Fire every configured webhook, swallowing all errors and never outlasting its budget.

    ``hint``: a configuration hint from the self-test, which each target may opt out of
    (WebhookConfig.config_hints).

    Everything is inside the guard, filter included: this runs on the engine's poll loop,
    which must never be brought down by a notification. The sends run concurrently and
    each is reported on its own, so one target that accepts the connection and then goes
    quiet cannot hold up the others — or the poll loop behind them.

    Bounded as well as guarded, and that half was missing. Engine._emit() awaits this from
    inside the poll loop: before the hosts are evaluated, before the cluster preparation
    starts, and between two shutdown stages. Per-target httpx timeouts do not bound the
    round — a read timeout bounds one read — so a target that answers a byte at a time
    used to spend the battery while the engine waited. NOTIFY_BUDGET_S is the ceiling;
    whatever has not answered by then is recorded as a timeout and abandoned.

    Still awaited rather than fired and forgotten, deliberately: the appliance shuts its
    own host down last and then loses power, so an event that has not been sent by the
    time we get there is an event nobody ever receives.
    """
    try:
        hooks = [
            h
            for h in (notifications.webhooks or [])
            if h.enabled and h.url and _passes(h, severity, hint)
        ]
        if not hooks:
            return
        # Each send carries the ceiling, rather than one wait_for around the gather. The
        # sends are concurrent, so the round still ends within the budget either way — but
        # this way a target that answered keeps its real outcome instead of being filed
        # under the timeout of the one that did not.
        results = await asyncio.gather(
            *(_send_bounded(h, subject, body, severity, payload) for h in hooks),
            return_exceptions=True,
        )
        for hook, result in zip(hooks, results):
            _record_delivery(hook, result)
    except Exception as exc:  # noqa: BLE001
        log.warning("Webhook notification failed: %s", exc)


async def _send_bounded(
    hook: WebhookConfig, subject: str, body: str, severity: str, payload: dict
) -> str:
    """One send, never longer than NOTIFY_BUDGET_S. Raises like send_webhook() does."""
    return await asyncio.wait_for(
        send_webhook(hook, subject, body, severity, payload), timeout=NOTIFY_BUDGET_S
    )
