"""SNMP poller for the UPS.

Reads the standard RFC 1628 UPS-MIB, which network UPS cards implement vendor-
independently. Supports SNMP v1/v2c (community) and v3 (authPriv). Pure-Python via
pysnmp, no external net-snmp binaries required.

A failed/timed-out poll yields ``reachable = False`` and never produces a
shutdown-worthy state: loss of SNMP communication is treated as an alarm, not as
a power failure (fail safe, not fail shutdown).

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .config import SnmpAuthProto, SnmpConfig, SnmpPrivProto, SnmpVersion

log = logging.getLogger("pve-usv.ups")

# --- RFC 1628 UPS-MIB OIDs ---------------------------------------------------
OID_IDENT_MANUFACTURER = "1.3.6.1.2.1.33.1.1.1.0"     # upsIdentManufacturer
OID_IDENT_MODEL = "1.3.6.1.2.1.33.1.1.2.0"            # upsIdentModel
OID_OUTPUT_SOURCE = "1.3.6.1.2.1.33.1.4.1.0"          # upsOutputSource
OID_BATTERY_STATUS = "1.3.6.1.2.1.33.1.2.1.0"         # upsBatteryStatus
OID_SECONDS_ON_BATTERY = "1.3.6.1.2.1.33.1.2.2.0"     # upsSecondsOnBattery
OID_MINUTES_REMAINING = "1.3.6.1.2.1.33.1.2.3.0"      # upsEstimatedMinutesRemaining
OID_CHARGE_REMAINING = "1.3.6.1.2.1.33.1.2.4.0"       # upsEstimatedChargeRemaining (%)

_ALL_OIDS = [
    OID_IDENT_MANUFACTURER,
    OID_IDENT_MODEL,
    OID_OUTPUT_SOURCE,
    OID_BATTERY_STATUS,
    OID_SECONDS_ON_BATTERY,
    OID_MINUTES_REMAINING,
    OID_CHARGE_REMAINING,
]

# upsOutputSource enum -> normalised power source string
_OUTPUT_SOURCE = {
    1: "other",
    2: "none",
    3: "mains",     # normal
    4: "bypass",
    5: "battery",
    6: "mains",     # booster (still on mains)
    7: "mains",     # reducer (still on mains)
}

# upsBatteryStatus enum -> normalised string
_BATTERY_STATUS = {
    1: "unknown",
    2: "normal",
    3: "low",
    4: "depleted",
}


@dataclass
class UpsState:
    reachable: bool = False
    last_poll: Optional[datetime] = None
    manufacturer: Optional[str] = None     # upsIdentManufacturer (as reported by the device)
    model: Optional[str] = None            # upsIdentModel (as reported by the device)
    power_source: str = "unknown"          # mains | battery | bypass | none | other | unknown
    battery_status: str = "unknown"        # normal | low | depleted | unknown
    seconds_on_battery: Optional[int] = None
    runtime_remaining_min: Optional[int] = None
    battery_charge_pct: Optional[int] = None
    error: Optional[str] = None
    raw: dict = field(default_factory=dict)

    @property
    def on_battery(self) -> bool:
        return self.power_source == "battery"

    @property
    def battery_low(self) -> bool:
        return self.battery_status in ("low", "depleted")


def _auth_protocol(proto: SnmpAuthProto):
    from pysnmp.hlapi.asyncio import (
        usmHMACMD5AuthProtocol,
        usmHMACSHAAuthProtocol,
        usmHMAC192SHA256AuthProtocol,
        usmHMAC384SHA512AuthProtocol,
        usmNoAuthProtocol,
    )

    return {
        SnmpAuthProto.none: usmNoAuthProtocol,
        SnmpAuthProto.md5: usmHMACMD5AuthProtocol,
        SnmpAuthProto.sha: usmHMACSHAAuthProtocol,
        SnmpAuthProto.sha256: usmHMAC192SHA256AuthProtocol,
        SnmpAuthProto.sha512: usmHMAC384SHA512AuthProtocol,
    }[proto]


def _priv_protocol(proto: SnmpPrivProto):
    from pysnmp.hlapi.asyncio import (
        usmDESPrivProtocol,
        usmAesCfb128Protocol,
        usmAesCfb256Protocol,
        usmNoPrivProtocol,
    )

    return {
        SnmpPrivProto.none: usmNoPrivProtocol,
        SnmpPrivProto.des: usmDESPrivProtocol,
        SnmpPrivProto.aes: usmAesCfb128Protocol,
        SnmpPrivProto.aes256: usmAesCfb256Protocol,
    }[proto]


def _auth_data(cfg: SnmpConfig):
    from pysnmp.hlapi.asyncio import CommunityData, UsmUserData

    if cfg.version in (SnmpVersion.v1, SnmpVersion.v2c):
        # mpModel 0 = SNMPv1, 1 = SNMPv2c
        mp = 0 if cfg.version == SnmpVersion.v1 else 1
        return CommunityData(cfg.community.get_secret_value(), mpModel=mp)

    return UsmUserData(
        cfg.v3_user,
        authKey=cfg.v3_auth_pass.get_secret_value() or None,
        privKey=cfg.v3_priv_pass.get_secret_value() or None,
        authProtocol=_auth_protocol(cfg.v3_auth_proto),
        privProtocol=_priv_protocol(cfg.v3_priv_proto),
    )


async def _make_transport(cfg: SnmpConfig):
    """Build a UDP transport target across pysnmp API variants."""
    from pysnmp.hlapi.asyncio import UdpTransportTarget

    addr = (cfg.host, cfg.port)
    kwargs = {"timeout": cfg.timeout_s, "retries": cfg.retries}
    # Newer pysnmp requires an async .create() (does non-blocking DNS resolution).
    create = getattr(UdpTransportTarget, "create", None)
    if create is not None:
        result = create(addr, **kwargs)
        if asyncio.iscoroutine(result):
            return await result
        return result
    return UdpTransportTarget(addr, **kwargs)


def _close_engine(engine) -> None:
    """Release the SnmpEngine's UDP socket + dispatcher after a poll.

    Without this, every poll would leak a UDP socket / asyncio transport: at the
    default cadence (~120 polls/h) the process file-descriptor limit fills up over a
    few hours and uvicorn can no longer accept connections — the web UI goes dark.

    pysnmp renamed the call: 6.x exposes ``transportDispatcher.closeDispatcher()``,
    7.x exposes ``engine.close_dispatcher()``. Try both, swallow everything: a failed
    cleanup must never crash the poller.
    """
    if engine is None:
        return
    try:
        closer = getattr(engine, "close_dispatcher", None)  # pysnmp 7.x
        if callable(closer):
            closer()
            return
        dispatcher = getattr(engine, "transportDispatcher", None)  # pysnmp 6.x
        if dispatcher is not None:
            dispatcher.closeDispatcher()
    except Exception as exc:  # noqa: BLE001 - cleanup is best effort only
        log.debug("Closing SNMP engine failed: %s", exc)


def _coerce_int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_str(value) -> Optional[str]:
    """DisplayString OID -> trimmed text, or None if empty/missing."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


async def poll(cfg: SnmpConfig) -> UpsState:
    """Poll the UPS once. Never raises; failures return reachable=False."""
    state = UpsState(last_poll=datetime.now(timezone.utc))

    if not cfg.configured:
        state.error = "SNMP not configured"
        return state

    engine = None
    try:
        from pysnmp.hlapi.asyncio import (
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
        )

        # Das GET-Kommando heisst in pysnmp 6.x `getCmd`, ab 7.x `get_cmd`
        # (gleiche Signatur und Rueckgabe). Beide Varianten unterstuetzen.
        try:
            from pysnmp.hlapi.asyncio import getCmd  # pysnmp 6.x
        except ImportError:
            from pysnmp.hlapi.asyncio import get_cmd as getCmd  # pysnmp 7.x

        engine = SnmpEngine()
        auth = _auth_data(cfg)
        transport = await _make_transport(cfg)
        objects = [ObjectType(ObjectIdentity(oid)) for oid in _ALL_OIDS]

        error_indication, error_status, error_index, var_binds = await getCmd(
            engine, auth, transport, ContextData(), *objects
        )

        if error_indication:
            state.error = str(error_indication)
            return state
        if error_status:
            state.error = f"{error_status.prettyPrint()} at index {error_index}"
            return state

        values: dict[str, object] = {}
        for var_bind in var_binds:
            oid, val = var_bind
            values[str(oid)] = val

        state.raw = {k: str(v) for k, v in values.items()}

        state.manufacturer = _coerce_str(values.get(OID_IDENT_MANUFACTURER))
        state.model = _coerce_str(values.get(OID_IDENT_MODEL))
        src = _coerce_int(values.get(OID_OUTPUT_SOURCE))
        bat = _coerce_int(values.get(OID_BATTERY_STATUS))
        state.power_source = _OUTPUT_SOURCE.get(src, "unknown")
        state.battery_status = _BATTERY_STATUS.get(bat, "unknown")
        state.seconds_on_battery = _coerce_int(values.get(OID_SECONDS_ON_BATTERY))
        state.runtime_remaining_min = _coerce_int(values.get(OID_MINUTES_REMAINING))
        state.battery_charge_pct = _coerce_int(values.get(OID_CHARGE_REMAINING))
        state.reachable = True
        return state

    except Exception as exc:  # noqa: BLE001 - poller must never crash the loop
        log.warning("SNMP poll failed: %s", exc)
        state.error = str(exc)
        return state

    finally:
        # Always release the engine's socket/dispatcher — especially on the common
        # timeout/unreachable path, which would otherwise leak fastest.
        _close_engine(engine)
