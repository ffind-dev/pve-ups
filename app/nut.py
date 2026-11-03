"""Network UPS Tools (NUT) client.

Reads a UPS from any ``upsd`` over TCP (default port 3493) — a NAS with its built-in UPS
server, a Raspberry Pi, an existing NUT box, or the driver bundled with this appliance.
That covers USB and serial UPS devices, which have no SNMP card to talk to.

We are strictly a *read-only client*: PVE-UPS never runs ``upsmon``, never registers as a
NUT primary/secondary and never lets NUT decide anything. NUT is used as a device driver;
the shutdown logic, the thresholds and the host policy stay here. Only ``LIST VAR`` is
ever sent, so a compromised appliance cannot command the UPS.

The protocol is a plain line-based TCP dialogue, so no extra dependency is needed:

    > LIST VAR myups
    < BEGIN LIST VAR myups
    < VAR myups ups.status "OL"
    < VAR myups battery.charge "100"
    < END LIST VAR myups

Like the SNMP poller, ``poll()`` never raises: a failure yields ``reachable = False``,
which is an alarm and never a shutdown (fail safe, not fail shutdown).

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import asyncio
import logging
import socket
from datetime import datetime, timezone
from typing import Optional

from .config import NutConfig
from .ups import ProbeEntry, ProbeResult, UpsState, redact_raw

log = logging.getLogger("pve-usv.nut")

# --- NUT variables -----------------------------------------------------------
# Names standardised by the NUT project; drivers implement them vendor-independently
# (the NUT counterpart of the RFC 1628 objects the SNMP poller reads).
VAR_STATUS = "ups.status"        # flag list: OL, OB, LB, ...
VAR_CHARGE = "battery.charge"    # percent
VAR_RUNTIME = "battery.runtime"  # SECONDS remaining (not minutes)
VAR_LOAD = "ups.load"            # percent of rated capacity (informational)
VAR_REALPOWER = "ups.realpower"  # watts (informational; many USB units omit it, #47)
VAR_REALPOWER_NOMINAL = "ups.realpower.nominal"  # rated watts, for the estimate
VAR_MFR = "device.mfr"
VAR_MODEL = "device.model"
VAR_MFR_LEGACY = "ups.mfr"       # pre-2.8 drivers
VAR_MODEL_LEGACY = "ups.model"

# Objects the manual test reports on, in display order.
# VAR_LOAD is listed for diagnostics only; it feeds no trigger (see _TRIGGER_VARS), so a
# driver that omits it shows as "missing" here without the wizard warning about anything.
# The two power variables are deliberately not listed: most USB drivers publish neither,
# and two "missing" rows on every such device would read as a fault. The dashboard says
# whether its watts are measured or estimated instead.
_PROBE_VARS = (VAR_STATUS, VAR_CHARGE, VAR_RUNTIME, VAR_LOAD, VAR_MFR, VAR_MODEL)

# Which variable each device-dependent trigger needs (see ups.PROBE_TRIGGERS).
_TRIGGER_VARS = {
    VAR_STATUS: ("on_battery", "battery_low"),
    VAR_RUNTIME: ("runtime",),
    VAR_CHARGE: ("charge",),
}

# ups.status flags -> normalised power source. Checked in this order: a UPS that says
# "OL BYPASS" is feeding the load past its inverter, which is not normal mains operation.
_POWER_FLAGS = (
    ("OB", "battery"),   # on battery
    ("BYPASS", "bypass"),
    ("OFF", "none"),     # output off
    ("OL", "mains"),     # on line, including TRIM/BOOST (still mains)
)

# Flags that mean "the battery will not last": LB is the driver's own low threshold, FSD
# is a forced shutdown another NUT master has already declared. Both are honoured as a
# low battery, i.e. they can only ever bring a shutdown forward, never cancel one.
_LOW_FLAGS = ("LB", "FSD")

# upsd error codes -> English text. Anything unknown is passed through verbatim.
_ERR_TEXT = {
    "ACCESS-DENIED": "upsd denied access (check the NUT user name and password)",
    "UNKNOWN-UPS": "upsd does not know a UPS with that name",
    "DATA-STALE": "upsd has stale data — its driver is not talking to the UPS",
    "DRIVER-NOT-CONNECTED": "upsd has no connection to the UPS driver",
    "PASSWORD-REQUIRED": "upsd requires a user name and password",
    "USERNAME-REQUIRED": "upsd requires a user name",
    "INVALID-ARGUMENT": "upsd rejected the request as invalid",
}

# upsd stays connected to one UPS with a few dozen variables; anything beyond this is a
# wrong port answering with an endless stream, and must not be read into memory forever.
_MAX_LINES = 1000


class _NutError(Exception):
    """upsd answered with ``ERR <code>``."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

    def __str__(self) -> str:
        return _ERR_TEXT.get(self.code, f"upsd error: {self.code}")


def _err_code(line: str) -> str:
    parts = line.split()
    return parts[1] if len(parts) > 1 else "UNKNOWN"


def _split_line(line: str) -> list[str]:
    """Split a upsd response line into tokens, honouring "quoted values" and backslashes."""
    tokens: list[str] = []
    buf: list[str] = []
    quoted = False
    escaped = False
    started = False
    for ch in line:
        if escaped:
            buf.append(ch)
            escaped = False
            started = True
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            quoted = not quoted
            started = True  # an empty "" is a real, empty token
        elif ch in " \t" and not quoted:
            if started:
                tokens.append("".join(buf))
                buf.clear()
                started = False
        else:
            buf.append(ch)
            started = True
    if started:
        tokens.append("".join(buf))
    return tokens


def _coerce_float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


class _Session:
    """One upsd connection. Every read is bounded by the configured timeout."""

    def __init__(self, reader, writer, timeout: float):
        self._reader = reader
        self._writer = writer
        self._timeout = max(0.5, float(timeout))

    async def _send(self, line: str) -> None:
        self._writer.write((line + "\n").encode("utf-8"))
        await asyncio.wait_for(self._writer.drain(), timeout=self._timeout)

    async def _readline(self) -> str:
        raw = await asyncio.wait_for(self._reader.readline(), timeout=self._timeout)
        if not raw:
            raise ConnectionError("upsd closed the connection")
        return raw.decode("utf-8", "replace").strip()

    @staticmethod
    def _require_ok(line: str) -> None:
        # Only ever inspects upsd's reply, never our own command — so a password can
        # not end up in an error message or the event log.
        if line.startswith("ERR "):
            raise _NutError(_err_code(line))
        if not line.startswith("OK"):
            raise ConnectionError(f"Unexpected upsd reply: {line[:80]}")

    async def login(self, username: str, password: str) -> None:
        """Authenticate — only needed on servers that restrict reading variables."""
        if not username:
            return
        await self._send(f"USERNAME {username}")
        self._require_ok(await self._readline())
        if password:
            await self._send(f"PASSWORD {password}")
            self._require_ok(await self._readline())

    async def list_var(self, ups_name: str) -> dict[str, str]:
        await self._send(f"LIST VAR {ups_name}")
        first = await self._readline()
        if first.startswith("ERR "):
            raise _NutError(_err_code(first))
        if not first.startswith("BEGIN LIST VAR"):
            raise ConnectionError(f"Unexpected upsd reply: {first[:80]}")

        variables: dict[str, str] = {}
        for _ in range(_MAX_LINES):
            line = await self._readline()
            if line.startswith("END LIST VAR"):
                return variables
            if line.startswith("ERR "):
                raise _NutError(_err_code(line))
            tokens = _split_line(line)
            if len(tokens) >= 4 and tokens[0] == "VAR":
                variables[tokens[2]] = tokens[3]
        raise ConnectionError("upsd sent an implausible amount of data")


async def _close(writer) -> None:
    """Close the connection, never raising — the counterpart of ups._close_engine()."""
    try:
        writer.write(b"LOGOUT\n")
        await writer.drain()
    except Exception:  # noqa: BLE001 - a dead socket must not mask the real result
        pass
    try:
        writer.close()
        await asyncio.wait_for(writer.wait_closed(), timeout=2.0)
    except Exception:  # noqa: BLE001
        pass


async def _read_variables(cfg: NutConfig) -> dict[str, str]:
    """Open, log in, read every variable, close. Raises on any protocol/transport error."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(cfg.host, cfg.port), timeout=max(0.5, cfg.timeout_s)
    )
    try:
        session = _Session(reader, writer, cfg.timeout_s)
        await session.login(cfg.username, cfg.password.get_secret_value())
        return await session.list_var(cfg.ups_name)
    finally:
        await _close(writer)


def _failure_text(cfg: NutConfig, exc: Exception) -> str:
    """English, secret-free description of why a read failed.

    The common transport failures get their own text rather than ``str(exc)``: the OS
    message is localised (so it would break the English-only event log) and says far less
    than it could — "connection refused" on 3493 almost always means upsd is not running
    or is listening on localhost only.
    """
    if isinstance(exc, _NutError):
        return str(exc)
    if isinstance(exc, asyncio.TimeoutError):
        return f"No response from {cfg.host}:{cfg.port} within {cfg.timeout_s:g} s"
    if isinstance(exc, ConnectionRefusedError):
        return (
            f"Connection refused by {cfg.host}:{cfg.port} — upsd is not running there, or "
            f"its LISTEN directive does not cover this interface"
        )
    if isinstance(exc, socket.gaierror):
        return f"Cannot resolve the host name '{cfg.host}'"
    if isinstance(exc, ConnectionResetError):
        return f"{cfg.host}:{cfg.port} closed the connection unexpectedly"
    return str(exc) or exc.__class__.__name__


def _apply_variables(state: UpsState, variables: dict[str, str]) -> None:
    """Map NUT variables onto the source-neutral UpsState."""
    # Redacted before it is stored, so every consumer inherits it. upsd publishes each
    # driver's configuration as driver.parameter.<name>, which for several drivers means a
    # plaintext password or SNMPv3 passphrase — and UpsState.raw ends up in the event log
    # via readable_raw(), which /api/status serves unauthenticated. The unmasked values are
    # never needed here: everything below reads a standardised ups./battery. variable.
    state.raw = redact_raw(variables)
    # upsd answered. Kept apart from ``reachable`` because the missing-ups.status branch
    # below is unreachable-but-not-silent, and the engine's pure communication-loss opt-in
    # must not fire on a server that is demonstrably talking to us (see UpsState.answered).
    state.answered = True
    state.manufacturer = variables.get(VAR_MFR) or variables.get(VAR_MFR_LEGACY) or None
    state.model = variables.get(VAR_MODEL) or variables.get(VAR_MODEL_LEGACY) or None

    charge = _coerce_float(variables.get(VAR_CHARGE))
    if charge is not None:
        state.battery_charge_pct = int(charge)
    load = _coerce_float(variables.get(VAR_LOAD))
    if load is not None:
        state.load_pct = int(load)
    watts = _coerce_float(variables.get(VAR_REALPOWER))
    if watts is not None and watts >= 0:
        state.power_w = int(round(watts))
    rated = _coerce_float(variables.get(VAR_REALPOWER_NOMINAL))
    if rated is not None and rated > 0:
        state.power_nominal_w = int(round(rated))
    runtime_s = _coerce_float(variables.get(VAR_RUNTIME))
    if runtime_s is not None:
        # NUT reports seconds. Round *down* so a threshold fires a moment early rather
        # than a moment late.
        state.runtime_remaining_min = int(runtime_s // 60)
    # NUT has no "seconds on battery" variable; the engine falls back to its own clock,
    # which it already does for SNMP cards that omit upsSecondsOnBattery.

    status = variables.get(VAR_STATUS)
    if status is None:
        # ups.status is mandatory for every NUT driver. Without it we cannot tell mains
        # from battery, and silently reporting "not on battery" would be the one way a
        # source could disable outage detection unnoticed. Stay unreachable instead:
        # that raises the usual alarm and never triggers a shutdown.
        state.error = f"upsd did not report {VAR_STATUS} — the driver is not delivering data"
        return

    # Kept verbatim next to the normalised word, for the same reason the SNMP path keeps
    # its enum: "low" alone cannot say whether the battery is empty or being replaced.
    state.battery_status_detail = f"ups.status={status.strip()}"
    flags = status.upper().split()
    state.power_source = next((src for flag, src in _POWER_FLAGS if flag in flags), "unknown")
    state.battery_status = "low" if any(f in flags for f in _LOW_FLAGS) else "normal"

    if state.power_source == "unknown":
        # The variable is there, but nothing in it says where the power comes from: an
        # empty value, or flags that describe something else entirely (CHRG, ALARM, RB, a
        # driver's intermediate state during a transfer). ``on_battery`` is
        # ``power_source == "battery"``, so leaving this reachable reads as MAINS —
        # mid-outage that clears the running on-battery timer, drops a latched trigger and
        # writes "mains power restored" about a device that reported nothing of the sort.
        # The same fail-dangerous default ups.py:_map_state() refuses for SNMP, and the
        # two sources have to answer this one question the same way. Unreachable is an
        # alarm, never a shutdown, and it keeps the blind countdown
        # (``keep_shutdown_on_comm_loss``) running on an outage that was already confirmed.
        state.error = (
            f"upsd reported {VAR_STATUS}='{status.strip()}', which names neither OL (on "
            f"line) nor OB (on battery), so it is unknown whether this UPS runs on mains "
            f"or on battery. Treated as unreachable — an alarm, never a shutdown. Use the "
            f"test button to see what the driver does deliver."
        )
        return

    state.reachable = True


async def poll(cfg: NutConfig) -> UpsState:
    """Poll the UPS once via upsd. Never raises; failures return reachable=False."""
    state = UpsState(last_poll=datetime.now(timezone.utc))

    if not cfg.configured:
        state.error = "NUT server not configured"
        return state

    try:
        variables = await _read_variables(cfg)
    except Exception as exc:  # noqa: BLE001 - poller must never crash the loop
        # This deliberately swallows ERR DATA-STALE / DRIVER-NOT-CONNECTED too: upsd
        # happily keeps serving the last known values when its driver has died, and
        # treating those as a valid reading would break the fail-safe contract.
        log.warning("NUT poll failed: %s", exc)
        # ...but an ERR is an ANSWER, and that is a different question from whether the
        # answer was usable (see UpsState.answered). upsd replying DATA-STALE,
        # DRIVER-NOT-CONNECTED, ACCESS-DENIED or UNKNOWN-UPS has demonstrably not stopped
        # talking to us, while a timeout, a refused connection or a DNS failure has.
        # engine._ups_trigger_reason() reads exactly that distinction: the
        # ``comm_loss_shutdown_after_min`` opt-in shuts the whole estate down on a *silent*
        # source, so filing a wedged driver or a mistyped ups_name under silence fired a
        # real shutdown during normal operation — and the alarm sent the operator to a
        # network that was working.
        state.answered = isinstance(exc, _NutError)
        state.error = _failure_text(cfg, exc)
        return state

    _apply_variables(state, variables)
    return state


def _describe(name: str, value: str) -> str:
    """Readable form of one probed variable; flags and units carry their meaning."""
    if name == VAR_STATUS:
        flags = value.upper().split()
        source = next((src for flag, src in _POWER_FLAGS if flag in flags), "unknown")
        battery = "low" if any(f in flags for f in _LOW_FLAGS) else "normal"
        return f"{source}, battery {battery}"
    if name == VAR_CHARGE:
        charge = _coerce_float(value)
        return f"{int(charge)} %" if charge is not None else value
    if name == VAR_RUNTIME:
        seconds = _coerce_float(value)
        return f"{int(seconds // 60)} min ({int(seconds)} s)" if seconds is not None else value
    return value or "(empty)"


def _probe_summary(cfg: NutConfig, result: ProbeResult, error: Optional[str]) -> str:
    """Short English overall diagnosis. Must never contain the NUT password."""
    where = f"{cfg.ups_name}@{cfg.host}:{cfg.port}"
    if not result.reachable:
        return (
            f"No usable answer from {where}: {error or 'upsd did not respond'}. Check the "
            f"address and port, the UPS name in upsd's ups.conf (upsc -l lists it), any "
            f"firewall on TCP {cfg.port}, and that upsd's LISTEN directive covers this host."
        )
    missing = [e.name for e in result.entries if e.status == "missing"]
    if not missing:
        return f"All {result.total} variables answered on {where}."
    parts = [f"{result.ok_count} of {result.total} variables answered on {where}."]
    parts.append("Not provided by this driver: " + ", ".join(missing) + ".")
    return " ".join(parts)


async def probe(cfg: NutConfig) -> ProbeResult:
    """Report every variable individually — manual test button only, never the poll loop.

    poll() only needs a handful of values and reports a single error string. The test
    button has to answer a different question: is the connection wrong, or does this
    particular driver simply not publish battery.runtime? One line per variable says so.
    Never raises.
    """
    result = ProbeResult(total=len(_PROBE_VARS))

    def _entries(status: str, error: Optional[str] = None) -> list[ProbeEntry]:
        return [ProbeEntry(name=name, status=status, error=error) for name in _PROBE_VARS]

    if not cfg.configured:
        result.entries = _entries("skipped")
        result.summary = "NUT server not configured (host and UPS name are required)."
        return result

    try:
        variables = await _read_variables(cfg)
    except Exception as exc:  # noqa: BLE001 - the test button must never 500
        log.warning("NUT probe failed: %s", exc)
        error = _failure_text(cfg, exc)
        # Stale data is a upsd that answers fine but whose driver is dead — a different
        # problem from "cannot connect", and worth its own label on every row.
        stale = isinstance(exc, _NutError) and exc.code in ("DATA-STALE", "DRIVER-NOT-CONNECTED")
        result.entries = _entries("stale" if stale else "error", error)
        result.summary = _probe_summary(cfg, result, error)
        return result

    for name in _PROBE_VARS:
        value = variables.get(name)
        if value is None and name in (VAR_MFR, VAR_MODEL):
            # Older drivers spell these ups.mfr / ups.model.
            legacy = VAR_MFR_LEGACY if name == VAR_MFR else VAR_MODEL_LEGACY
            value = variables.get(legacy)
        if value is None:
            result.entries.append(ProbeEntry(name=name, status="missing"))
            continue
        result.entries.append(
            ProbeEntry(name=name, status="ok", raw=value, value=_describe(name, value))
        )

    result.ok_count = sum(1 for e in result.entries if e.status == "ok")
    result.reachable = True  # upsd answered; a variable it lacks is a different problem
    answered = {e.name for e in result.entries if e.status == "ok"}
    result.missing_triggers = [
        trigger
        for var, triggers in _TRIGGER_VARS.items()
        if var not in answered
        for trigger in triggers
    ]
    result.summary = _probe_summary(cfg, result, None)
    return result
