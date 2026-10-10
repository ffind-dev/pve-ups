"""SNMP poller for the UPS, plus the source-neutral state objects.

Reads the standard RFC 1628 UPS-MIB, which network UPS cards implement vendor-
independently — or a vendor MIB where the standard is not enough. Which one is a per-UPS
setting (``SnmpConfig.mib``); the default ``auto`` reads RFC 1628 and switches to a vendor
MIB when the device answers it. See ``MibProfile`` below. Supports SNMP v1/v2c (community)
and v3 (authPriv). Pure-Python via pysnmp, no external net-snmp binaries required.

``UpsState``, ``ProbeEntry`` and ``ProbeResult`` live here but are *not* SNMP-specific:
they are the contract every UPS source produces (see app/sources.py for the dispatch and
app/nut.py for the second implementation). The engine only ever sees these, which is why
the whole trigger/policy/fail-safe machinery is transport-independent.

A failed/timed-out poll yields ``reachable = False`` and never produces a
shutdown-worthy state: loss of communication with the UPS is treated as an alarm, not as
a power failure (fail safe, not fail shutdown).

Copyright 2026 Florian Finder
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .config import SnmpAuthProto, SnmpConfig, SnmpMib, SnmpPrivProto, SnmpVersion

log = logging.getLogger("pve-usv.ups")

# --- MIB profiles ------------------------------------------------------------
# A profile is one way to read a UPS over SNMP: a table of scalar objects plus how each
# of them becomes a UpsState field. RFC 1628 is the vendor-independent standard and stays
# the default. Vendor MIBs exist because devices implement the standard incompletely or
# not at all — see the APC profile for a documented example. Adding a vendor is a table
# here, a member in config.SnmpMib and two i18n keys; no other module changes.

# How a raw value becomes a UpsState field. "ticks_*" are SNMP TimeTicks, i.e. hundredths
# of a second — the single most common misreading of vendor MIBs.
KIND_TEXT = "text"          # DisplayString -> trimmed text
KIND_INT = "int"            # plain integer
KIND_PCT = "pct"            # integer percent
KIND_SECONDS = "seconds"    # integer seconds
KIND_TICKS_S = "ticks_s"    # TimeTicks -> seconds
KIND_TICKS_MIN = "ticks_min"  # TimeTicks -> whole minutes
KIND_ENUM = "enum"          # integer -> normalised string via MibObject.enum


@dataclass(frozen=True)
class MibObject:
    """One scalar object of a MIB profile."""

    # Scalars are ".0"-suffixed. A table column may be addressed with a FIXED index
    # (e.g. upsOutputPercentLoad on output line 1) — what we never do is walk a table.
    oid: str
    name: str                       # as spelled in the MIB, shown by the manual probe
    field: Optional[str] = None     # UpsState attribute this fills (None = not stored)
    kind: str = KIND_INT
    enum: Optional[dict] = None     # KIND_ENUM: device value -> normalised string
    trigger: Optional[str] = None   # member of PROBE_TRIGGERS this object feeds


@dataclass(frozen=True)
class MibProfile:
    """One readable MIB: which objects to GET and what they mean."""

    id: str                             # config.SnmpMib value, e.g. "rfc1628"
    label: str                          # English display name, used in probe summaries
    objects: tuple[MibObject, ...]
    anchor: str                         # OID whose answer proves this MIB is implemented
    manufacturer: Optional[str] = None  # constant when the MIB has no manufacturer object
    # One GET per object instead of one GET for the whole profile. For agents that time
    # out on a request carrying several objects although each of them answers on its own
    # (seen on CyberPower cards, #40). Costs a round trip per object — a few hundred
    # milliseconds on a LAN — but never a second timeout: the first transport error ends
    # the poll exactly as the single multi-object GET would have.
    single_varbind: bool = False

    @property
    def oids(self) -> list[str]:
        return [o.oid for o in self.objects]

    @property
    def trigger_oids(self) -> dict[str, str]:
        """OID -> trigger, in the order the wizard lists them."""
        return {o.oid: o.trigger for o in self.objects if o.trigger}

    @property
    def source_object(self) -> Optional[MibObject]:
        """The object that says whether the UPS runs on mains or on battery.

        Named rather than assumed, because _map_state() has to be able to tell "the
        device did not answer this one" apart from every other missing object: every
        other field degrades into a threshold that simply cannot fire, while this one
        decides the whole state machine. Every profile has exactly one.
        """
        return next((o for o in self.objects if o.field == "power_source"), None)


# --- RFC 1628 UPS-MIB --------------------------------------------------------
OID_IDENT_MANUFACTURER = "1.3.6.1.2.1.33.1.1.1.0"     # upsIdentManufacturer
OID_IDENT_MODEL = "1.3.6.1.2.1.33.1.1.2.0"            # upsIdentModel
OID_OUTPUT_SOURCE = "1.3.6.1.2.1.33.1.4.1.0"          # upsOutputSource
OID_BATTERY_STATUS = "1.3.6.1.2.1.33.1.2.1.0"         # upsBatteryStatus
OID_SECONDS_ON_BATTERY = "1.3.6.1.2.1.33.1.2.2.0"     # upsSecondsOnBattery
OID_MINUTES_REMAINING = "1.3.6.1.2.1.33.1.2.3.0"      # upsEstimatedMinutesRemaining
OID_CHARGE_REMAINING = "1.3.6.1.2.1.33.1.2.4.0"       # upsEstimatedChargeRemaining (%)
# upsOutputPercentLoad lives in upsOutputTable, indexed by output line. We read line 1 —
# the only line on a single-phase UPS, and the representative one otherwise. A fixed
# index, not a walk.
OID_OUTPUT_LOAD = "1.3.6.1.2.1.33.1.4.4.1.5.1"        # upsOutputPercentLoad.1

# upsOutputSource enum -> normalised power source string
_OUTPUT_SOURCE = {
    # other(1) is the MIB's own "none of the below", i.e. a device saying it cannot
    # classify its output — which is the same statement as unknown, and has to be read the
    # same way (see _map_state): unreachable, an alarm, never a shutdown, and a running
    # on-battery timer left alone. Mapped to "other" it was simply "not battery", so a
    # card that fell back to it mid-outage cleared the timer and logged "mains power
    # restored". Only the RFC 1628 table: APC's own enum uses "other" for rebooting(8),
    # which is a definite state and stays one.
    1: "unknown",
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

RFC1628 = MibProfile(
    id="rfc1628",
    label="RFC 1628",
    anchor=OID_OUTPUT_SOURCE,
    objects=(
        MibObject(OID_IDENT_MANUFACTURER, "upsIdentManufacturer", "manufacturer", KIND_TEXT),
        MibObject(OID_IDENT_MODEL, "upsIdentModel", "model", KIND_TEXT),
        MibObject(OID_OUTPUT_SOURCE, "upsOutputSource", "power_source", KIND_ENUM,
                  enum=_OUTPUT_SOURCE, trigger="on_battery"),
        MibObject(OID_BATTERY_STATUS, "upsBatteryStatus", "battery_status", KIND_ENUM,
                  enum=_BATTERY_STATUS, trigger="battery_low"),
        MibObject(OID_SECONDS_ON_BATTERY, "upsSecondsOnBattery", "seconds_on_battery",
                  KIND_SECONDS),
        MibObject(OID_MINUTES_REMAINING, "upsEstimatedMinutesRemaining",
                  "runtime_remaining_min", KIND_INT, trigger="runtime"),
        MibObject(OID_CHARGE_REMAINING, "upsEstimatedChargeRemaining", "battery_charge_pct",
                  KIND_PCT, trigger="charge"),
        # Informational only (no trigger): a device without it must not make the wizard
        # warn about an unavailable shutdown condition.
        MibObject(OID_OUTPUT_LOAD, "upsOutputPercentLoad", "load_pct", KIND_PCT),
    ),
)

# --- APC PowerNet-MIB (enterprise 318) ---------------------------------------
# Schneider only supports RFC 1628 on Network Management Card 2 (AP9630/AP9631/AP9635)
# from firmware sumx/sy v5.1.7 on; the older NMC1 cards (AP9617/AP9618/AP9619) speak
# PowerNet only and are SNMPv1-only, which is why the "auto" fallback below must work
# under v1 as well.
OID_APC_MODEL = "1.3.6.1.4.1.318.1.1.1.1.1.1.0"           # upsBasicIdentModel
OID_APC_OUTPUT_STATUS = "1.3.6.1.4.1.318.1.1.1.4.1.1.0"   # upsBasicOutputStatus
OID_APC_BATTERY_STATUS = "1.3.6.1.4.1.318.1.1.1.2.1.1.0"  # upsBasicBatteryStatus
OID_APC_TIME_ON_BATTERY = "1.3.6.1.4.1.318.1.1.1.2.1.2.0"  # upsBasicBatteryTimeOnBattery
OID_APC_CAPACITY = "1.3.6.1.4.1.318.1.1.1.2.2.1.0"        # upsAdvBatteryCapacity
OID_APC_LOAD = "1.3.6.1.4.1.318.1.1.1.4.2.3.0"            # upsAdvOutputLoad (% of rated)
OID_APC_RUNTIME = "1.3.6.1.4.1.318.1.1.1.2.2.3.0"         # upsAdvBatteryRunTimeRemaining

# upsBasicOutputStatus enum -> normalised power source string.
#
# onBatteryTest(15) maps to "mains" on purpose: during a self-test — which this appliance
# schedules itself — the UPS genuinely runs off the battery, and RFC 1628 cannot tell that
# apart from an outage (it reports upsOutputSource = battery either way). The APC MIB can,
# so a self-test never starts the on-battery timer here.
_APC_OUTPUT_STATUS = {
    1: "unknown",
    2: "mains",     # onLine
    3: "battery",   # onBattery
    4: "mains",     # onSmartBoost (still on mains, boosting a low input voltage)
    5: "none",      # timedSleeping
    6: "bypass",    # softwareBypass
    7: "none",      # off
    8: "other",     # rebooting
    9: "bypass",    # switchedBypass
    10: "bypass",   # hardwareFailureBypass
    11: "none",     # sleepingUntilPowerReturn
    12: "mains",    # onSmartTrim (still on mains, trimming a high input voltage)
    13: "mains",    # ecoMode
    14: "mains",    # hotStandby
    15: "mains",    # onBatteryTest — a self-test, not an outage (see above)
    16: "bypass",   # emergencyStaticBypass
    17: "bypass",   # staticBypassStandby
    18: "mains",    # powerSavingMode
    19: "mains",    # spotMode
    20: "mains",    # eConversion
    21: "mains",    # chargingMode
}

# upsBasicBatteryStatus enum -> normalised string. A battery in fault condition or a
# missing battery counts as "low", following NUT's apc-mib.c: the engine only ever acts on
# battery_low while the UPS is *also* on battery, where both really are an emergency.
_APC_BATTERY_STATUS = {
    1: "unknown",
    2: "normal",
    3: "low",       # batteryLow
    4: "low",       # batteryInFaultCondition
    5: "low",       # noBatteryPresent
}

APC = MibProfile(
    id="apc",
    label="APC PowerNet",
    anchor=OID_APC_OUTPUT_STATUS,
    # PowerNet has no manufacturer object — enterprise 318 already says who built it.
    manufacturer="APC",
    objects=(
        MibObject(OID_APC_MODEL, "upsBasicIdentModel", "model", KIND_TEXT),
        MibObject(OID_APC_OUTPUT_STATUS, "upsBasicOutputStatus", "power_source", KIND_ENUM,
                  enum=_APC_OUTPUT_STATUS, trigger="on_battery"),
        MibObject(OID_APC_BATTERY_STATUS, "upsBasicBatteryStatus", "battery_status",
                  KIND_ENUM, enum=_APC_BATTERY_STATUS, trigger="battery_low"),
        MibObject(OID_APC_TIME_ON_BATTERY, "upsBasicBatteryTimeOnBattery",
                  "seconds_on_battery", KIND_TICKS_S),
        MibObject(OID_APC_RUNTIME, "upsAdvBatteryRunTimeRemaining", "runtime_remaining_min",
                  KIND_TICKS_MIN, trigger="runtime"),
        MibObject(OID_APC_CAPACITY, "upsAdvBatteryCapacity", "battery_charge_pct", KIND_PCT,
                  trigger="charge"),
        MibObject(OID_APC_LOAD, "upsAdvOutputLoad", "load_pct", KIND_PCT),
    ),
)

# --- CyberPower CPS-MIB (enterprise 3808) ------------------------------------
# The RMCARD family. Some of these cards implement no RFC 1628 at all, and at least one
# firmware does not answer a GET carrying several objects — not even with an error, it
# simply times out — while every object answers when asked on its own (#40). Hence
# single_varbind. Cards that are rebranded CyberPower hardware answer the same MIB.
OID_CPS_MODEL = "1.3.6.1.4.1.3808.1.1.1.1.1.1.0"            # upsBaseIdentModel
OID_CPS_OUTPUT_STATUS = "1.3.6.1.4.1.3808.1.1.1.4.1.1.0"    # upsBaseOutputStatus
OID_CPS_BATTERY_STATUS = "1.3.6.1.4.1.3808.1.1.1.2.1.1.0"   # upsBaseBatteryStatus
OID_CPS_TIME_ON_BATTERY = "1.3.6.1.4.1.3808.1.1.1.2.1.2.0"  # upsBaseBatteryTimeOnBattery
OID_CPS_CAPACITY = "1.3.6.1.4.1.3808.1.1.1.2.2.1.0"         # upsAdvanceBatteryCapacity
# .2.2.4, not APC's .2.2.3 — the two MIBs look alike but are not interchangeable.
OID_CPS_RUNTIME = "1.3.6.1.4.1.3808.1.1.1.2.2.4.0"          # upsAdvanceBatteryRunTimeRemaining
OID_CPS_LOAD = "1.3.6.1.4.1.3808.1.1.1.4.2.3.0"             # upsAdvanceOutputLoad (%)

# upsBaseOutputStatus enum -> normalised power source string. Boost, buck, ECO and
# overload all still run on mains; sleep and off deliver no output at all.
_CPS_OUTPUT_STATUS = {
    1: "unknown",
    2: "mains",     # onLine
    3: "battery",   # onBattery
    4: "mains",     # onBoost (still on mains, boosting a low input voltage)
    5: "none",      # onSleep
    6: "none",      # off
    7: "other",     # rebooting
    8: "mains",     # onECO
    9: "bypass",    # onBypass
    10: "mains",    # onBuck (still on mains, trimming a high input voltage)
    11: "mains",    # onOverload (on mains, but overloaded)
}

# upsBaseBatteryStatus enum -> normalised string. A missing battery counts as "low", as
# in the APC profile: the engine only acts on it while the UPS is also on battery.
_CPS_BATTERY_STATUS = {
    1: "unknown",
    2: "normal",    # batteryNormal
    3: "low",       # batteryLow
    4: "low",       # batteryNotPresent
}

CYBERPOWER = MibProfile(
    id="cyberpower",
    label="CyberPower",
    anchor=OID_CPS_OUTPUT_STATUS,
    # Like PowerNet, the CPS-MIB has no manufacturer object.
    manufacturer="CyberPower",
    single_varbind=True,
    objects=(
        MibObject(OID_CPS_MODEL, "upsBaseIdentModel", "model", KIND_TEXT),
        MibObject(OID_CPS_OUTPUT_STATUS, "upsBaseOutputStatus", "power_source", KIND_ENUM,
                  enum=_CPS_OUTPUT_STATUS, trigger="on_battery"),
        MibObject(OID_CPS_BATTERY_STATUS, "upsBaseBatteryStatus", "battery_status",
                  KIND_ENUM, enum=_CPS_BATTERY_STATUS, trigger="battery_low"),
        MibObject(OID_CPS_TIME_ON_BATTERY, "upsBaseBatteryTimeOnBattery",
                  "seconds_on_battery", KIND_TICKS_S),
        MibObject(OID_CPS_RUNTIME, "upsAdvanceBatteryRunTimeRemaining",
                  "runtime_remaining_min", KIND_TICKS_MIN, trigger="runtime"),
        MibObject(OID_CPS_CAPACITY, "upsAdvanceBatteryCapacity", "battery_charge_pct",
                  KIND_PCT, trigger="charge"),
        MibObject(OID_CPS_LOAD, "upsAdvanceOutputLoad", "load_pct", KIND_PCT),
    ),
)

DEFAULT_PROFILE = RFC1628

# Vendor profiles come after the standard: in "auto" mode the last profile whose anchor
# answers wins, so a device that speaks both is read on the more precise vendor MIB.
PROFILES: dict[str, MibProfile] = {
    RFC1628.id: RFC1628, APC.id: APC, CYBERPOWER.id: CYBERPOWER,
}

# Flat OID -> object registry across all profiles (OIDs are globally unique), so the probe
# can name and interpret any object without knowing which profile it came from.
_OBJECTS: dict[str, MibObject] = {o.oid: o for p in PROFILES.values() for o in p.objects}

# Closed set of per-object probe outcomes, shared by every UPS source. The UI labels each
# one via the i18n key "probe.st.<status>", so a new status here needs a matching key in
# both dictionaries (app/web/i18n/en.js + de.js) — tests/test_i18n.py enforces that.
# The four SNMP-flavoured ones are only ever emitted by this module; "missing"/"stale"
# are their equivalents for sources that answer with named variables (see app/nut.py).
PROBE_STATUSES = (
    "ok",
    "noSuchObject",
    "noSuchInstance",
    "endOfMibView",
    "noSuchName",
    "missing",
    "stale",
    "error",
    "skipped",
)

# Trigger conditions whose availability depends on what the device actually reports. A
# probe lists the ones it could not read, so the wizard can warn instead of leaving the
# user with a threshold that will never fire. The UI labels each via "probe.trg.<name>".
# ``on_battery`` is outage detection itself; the on_battery_seconds timer runs on our own
# clock and therefore always works, which is why it is not listed here.
PROBE_TRIGGERS = ("on_battery", "battery_low", "runtime", "charge")

# v2c/v3 report a missing object as a per-varbind sentinel value instead of an error.
# Matched by class name, not isinstance: the classes moved between pysnmp 6.x and 7.x,
# and an unrecognised sentinel must not be mistaken for a real value.
_MISSING_SENTINELS = {
    "NoSuchObject": "noSuchObject",
    "NoSuchInstance": "noSuchInstance",
    "EndOfMibView": "endOfMibView",
}


# Keys whose VALUE must never be recorded, whatever the source produced them.
#
# Not a theoretical precaution. readable_raw() below is written into the event log by
# engine._log_trigger_evidence(), and /api/status serves 48 h of that log without
# authentication — while NUT hands back its entire ``LIST VAR`` answer, which for a number
# of drivers includes driver.parameter.password, .authPassword, .privPassword and
# .community. That is the same class of leak this release closed for the webhook URL in
# notify.safe_error(), on the evidence path instead of the delivery path.
# "pass" everywhere EXCEPT after "by". NUT publishes input.bypass.voltage, .frequency,
# .current and .realpower as standard variables, and a bare "pass" matches "bypass" — so
# the evidence line masked exactly the mains readings it exists to record. The lookbehind
# is deliberately the only narrowing: everything a driver might spell "pass", "password",
# "authPassword" or "passphrase" still matches, because erring towards masking is free
# here and erring the other way publishes a credential.
_SECRET_KEY_RE = re.compile(
    r"((?<!by)pass|secret|token|community|authkey|privkey)", re.I
)
SECRET_MASK = "**********"


def redact_raw(raw: dict) -> dict:
    """Copy of ``raw`` with every secret-looking value replaced by the mask.

    Masked rather than dropped: that a driver carries a password at all is itself
    diagnostic information, and a missing key would read as a driver that has none. The
    same placeholder the web API uses for a stored secret, so it is already familiar.
    """
    return {
        key: (SECRET_MASK if _SECRET_KEY_RE.search(str(key)) else value)
        for key, value in raw.items()
    }


@dataclass
class UpsState:
    reachable: bool = False
    # Whether the device answered at all, which is a different question from whether the
    # answer was usable. Both "it answered but names no power source" and "it answered but
    # implements none of this MIB's objects" are reachable=False — correctly, because the
    # state machine has nothing to go on — but they are NOT a communication loss, and
    # engine._ups_trigger_reason() reads exactly that distinction: the
    # ``comm_loss_shutdown_after_min`` opt-in shuts the estate down on a *silent* device,
    # and concluding "silent" from an answer would fire it during normal operation.
    answered: bool = False
    last_poll: Optional[datetime] = None
    manufacturer: Optional[str] = None     # upsIdentManufacturer (as reported by the device)
    model: Optional[str] = None            # upsIdentModel (as reported by the device)
    power_source: str = "unknown"          # mains | battery | bypass | none | other | unknown
    battery_status: str = "unknown"        # normal | low | depleted | unknown
    # What the device actually said, e.g. "upsBasicBatteryStatus=4" or "ups.status=OB LB".
    # The normalised word above loses the distinction that matters when this fires a
    # shutdown: APC maps batteryLow(3), batteryInFaultCondition(4) and noBatteryPresent(5)
    # all to "low", and an event log reading "UPS reports 'low'" cannot tell an empty
    # battery from a defective one. Diagnostics only — no trigger reads it.
    battery_status_detail: str = ""
    seconds_on_battery: Optional[int] = None
    runtime_remaining_min: Optional[int] = None
    battery_charge_pct: Optional[int] = None
    # Output load in percent of the UPS's rated capacity. Informational only: no trigger
    # reads it, so a device that does not report it simply shows nothing.
    load_pct: Optional[int] = None
    error: Optional[str] = None
    raw: dict = field(default_factory=dict)
    # Which MIB profile produced this state ("" for sources without one, e.g. NUT).
    # Diagnostics only: no trigger ever reads it, but "auto" would be opaque without it.
    mib: str = ""

    @property
    def on_battery(self) -> bool:
        return self.power_source == "battery"

    @property
    def battery_low(self) -> bool:
        return self.battery_status in ("low", "depleted")

    def readable_raw(self, limit: int = 1000) -> str:
        """The last poll's values as one line of evidence: ``name=value, ...``.

        SNMP keys are OIDs, which nobody can read at a glance; NUT keys are variable names,
        which everybody can. Both go through the same registry, so a key it does not know
        simply keeps its own spelling. This is written into the event log when a trigger
        fires: the readings that caused a shutdown are precisely what an incident report
        needs afterwards, and the manual test button can only ever show a later, different
        state.

        Capped, because the two sources are not the same size. SNMP contributes the profile's
        handful of OIDs; NUT hands back its whole ``LIST VAR`` answer, which on a real device
        is fifty to ninety variables and several kilobytes. That body goes into SQLite, into
        the event table of the UI and into the public status endpoint, so an unbounded line
        would drown the feed at the exact moment it matters most. The battery values lead,
        since they are the ones a post-mortem starts from; the complete answer is a click
        away in the card's test button.
        """
        if not self.raw:
            return ""

        def rank(key: str) -> tuple[int, str]:
            name = _object_name(key)
            lead = ("ups.status", "battery", "ups.timer", "ups")
            return (next((i for i, p in enumerate(lead) if name.startswith(p)), len(lead)),
                    name)

        # Redacted again here, not only at the source. nut.py already masks what it
        # stores, but this is the method whose output reaches the public endpoint, and a
        # future source that fills ``raw`` itself must not have to remember the rule.
        safe = redact_raw(self.raw)
        parts = [f"{_object_name(k)}={safe[k]}" for k in sorted(safe, key=rank)]
        out = ""
        for i, part in enumerate(parts):
            candidate = part if not out else f"{out}, {part}"
            if len(candidate) > limit:
                return f"{out}, … (+{len(parts) - i} more)" if out else part[:limit]
            out = candidate
        return out


@dataclass
class ProbeEntry:
    """Outcome of reading one object during the manual UPS test."""

    name: str                       # object name, e.g. "upsOutputSource" or "ups.status"
    status: str                     # one of PROBE_STATUSES
    oid: str = ""                   # SNMP OID; empty for sources without one
    value: Optional[str] = None     # interpreted value, e.g. "battery (5)" (status "ok")
    raw: Optional[str] = None       # value as the device spelled it (status "ok")
    error: Optional[str] = None     # English error text (status "error")


@dataclass
class ProbeResult:
    """Per-object diagnosis of one UPS. Manual test button only, never the poll loop."""

    reachable: bool = False         # at least one object answered
    summary: str = ""               # short English overall diagnosis, free of secrets
    # ok_count/total count the *resolved* profile's objects only. In "auto" mode the
    # entries of the MIB that lost are still listed (that is the diagnostic value), but
    # counting them would report "6 of 13" on a perfectly healthy device.
    ok_count: int = 0
    total: int = 0
    mib: str = ""                   # id of the profile the poll will use
    entries: list[ProbeEntry] = field(default_factory=list)
    # Trigger conditions this device cannot feed (subset of PROBE_TRIGGERS). Only
    # meaningful when ``reachable`` — an unreachable UPS reports nothing at all.
    missing_triggers: list[str] = field(default_factory=list)


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


# Shown instead of pysnmp's "Ciphering services not available", which reads like a network
# problem although not a single packet leaves the box.
PRIVACY_MISSING = (
    "SNMPv3 privacy (encryption) is not available in this installation: the Python package "
    "'cryptography' is missing, and pysnmp delegates DES/AES to it. Update the appliance — "
    "the package is installed automatically — or set this UPS user to authNoPriv "
    "(privacy 'none') in the meantime. Authentication itself is unaffected."
)


def _privacy_unavailable(cfg: SnmpConfig) -> bool:
    """True when this UPS needs SNMPv3 privacy but the ciphers are not installed.

    Checked up front rather than by matching pysnmp's error text: the failure is purely
    local (nothing is sent), and the generic "unreachable" wording sends users hunting for
    firewalls. Never raises.
    """
    if cfg.version != SnmpVersion.v3 or cfg.v3_priv_proto == SnmpPrivProto.none:
        return False
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher  # noqa: F401

        if cfg.v3_priv_proto == SnmpPrivProto.des:
            # DES lives in the "decrepit" module, which only exists from cryptography 43.
            from cryptography.hazmat.decrepit.ciphers import algorithms  # noqa: F401
    except Exception:  # noqa: BLE001 - any import trouble means "cannot encrypt"
        return True
    return False


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


def _pretty(value) -> str:
    """Value as the device spelled it, across pysnmp types that lack prettyPrint()."""
    printer = getattr(value, "prettyPrint", None)
    return printer() if callable(printer) else str(value)


def _usable(value) -> bool:
    """True when a varbind carries a real value, not a v2c/v3 "missing object" sentinel."""
    return value is not None and type(value).__name__ not in _MISSING_SENTINELS


def _object_name(oid: str) -> str:
    """MIB name of an OID across all profiles, so a user can look it up in the vendor doc."""
    obj = _OBJECTS.get(oid)
    return obj.name if obj else oid


def _convert(obj: MibObject, value):
    """Raw SNMP value -> the UpsState field's type, or None when it cannot be read."""
    if obj.kind == KIND_TEXT:
        return _coerce_str(value)
    num = _coerce_int(value)
    if obj.kind == KIND_ENUM:
        return (obj.enum or {}).get(num, "unknown")
    if num is None:
        return None
    # TimeTicks are hundredths of a second. Rounding down is deliberate: a runtime of
    # 9.9 minutes must not be reported as 10 and clear a "below 10 minutes" threshold.
    if obj.kind == KIND_TICKS_S:
        return num // 100
    if obj.kind == KIND_TICKS_MIN:
        return num // 6000
    return num


def _interpret(oid: str, value) -> str:
    """Readable form of one probed value: enums carry their meaning, text is trimmed."""
    obj = _OBJECTS.get(oid)
    if obj is None:
        num = _coerce_int(value)
        return str(num) if num is not None else _pretty(value)
    if obj.kind == KIND_ENUM:
        num = _coerce_int(value)
        if num is None:
            return _pretty(value)
        return f"{(obj.enum or {}).get(num, 'unknown')} ({num})"
    if obj.kind == KIND_TEXT:
        return _coerce_str(value) or "(empty)"
    num = _coerce_int(value)
    if num is None:
        return _pretty(value)
    # Show the converted number for TimeTicks; ProbeEntry.raw still carries the original,
    # so a user can see both and spot a unit problem at a glance.
    if obj.kind == KIND_TICKS_S:
        return f"{num // 100} s"
    if obj.kind == KIND_TICKS_MIN:
        return f"{num // 6000} min"
    return str(num)


def _selected_mib(cfg: SnmpConfig) -> SnmpMib:
    """The configured MIB, tolerating a config object that predates the field."""
    return getattr(cfg, "mib", SnmpMib.auto) or SnmpMib.auto


def _profiles_for(cfg: SnmpConfig) -> list[MibProfile]:
    """Profiles to consider, in query order: the standard first, vendor MIBs after."""
    mib = _selected_mib(cfg)
    if mib == SnmpMib.auto:
        return [DEFAULT_PROFILE] + [p for p in PROFILES.values() if p is not DEFAULT_PROFILE]
    return [PROFILES[mib.value]]


def _map_state(state: UpsState, profile: MibProfile, values: dict, auto: bool = False) -> None:
    """Fill a UpsState from one profile's varbinds. Objects the device left out keep
    their default, which is the same "unknown"/None a missing OID has always produced."""
    state.raw = {k: str(v) for k, v in values.items()}
    # Unconditional: this function is only ever reached with a reply in hand, and two of
    # the three verdicts below end in reachable=False. Only this flag keeps those apart
    # from a device that is not there at all — see UpsState.answered.
    state.answered = True
    state.mib = profile.id
    state.manufacturer = profile.manufacturer
    answered = 0
    for obj in profile.objects:
        if not _usable(values.get(obj.oid)):
            continue
        answered += 1
        if obj.field is None:
            continue
        converted = _convert(obj, values[obj.oid])
        if converted is not None:
            setattr(state, obj.field, converted)
            if obj.field == "battery_status":
                state.battery_status_detail = f"{obj.name}={_coerce_int(values[obj.oid])}"

    if answered and state.power_source != "unknown":
        state.reachable = True
        return

    if answered:
        # Objects answered, but not the one that says where the power comes from — it was
        # absent, unconvertible, or (APC's upsBasicOutputStatus) reported unknown(1).
        # Everything else degrades into a threshold that cannot fire; this one decides the
        # state machine, and ``on_battery`` is ``power_source == "battery"``, so leaving it
        # at "unknown" reads as MAINS. Mid-outage that clears the running on-battery timer,
        # drops a latched trigger and writes "mains power restored" — the one fail-dangerous
        # reading in the whole engine. app/nut.py has always refused this (no ups.status ->
        # unreachable); the two sources have to answer the same question the same way.
        # Unreachable is an alarm, never a shutdown, and it keeps the blind countdown
        # (``keep_shutdown_on_comm_loss``) running on a confirmed outage.
        source = profile.source_object
        state.reachable = False
        state.error = (
            f"The device answered, but {source.name if source else 'the power source'} did "
            f"not yield a usable power source, so it is unknown whether this UPS runs on "
            f"mains or on battery. Treated as unreachable — an alarm, never a shutdown. "
            f"Use the test button to see which objects it does implement."
        )
        return

    # The agent replied, but not one object of this MIB exists on it — a UPS pinned to the
    # wrong MIB, or an SNMP device that is not a UPS at all. Reporting that as reachable
    # would show a healthy-looking card with no data and silence every trigger, so treat it
    # as a communication failure: an alarm, never a shutdown.
    state.reachable = False
    state.error = (
        "The device answered, but implements neither RFC 1628 nor a supported vendor MIB. "
        "Check that this address really is the UPS network card."
        if auto else
        f"The device answered, but implements none of the {profile.label} objects. "
        f"Set this UPS's MIB to 'auto', or to the MIB the device actually provides."
    )


async def poll(cfg: SnmpConfig) -> UpsState:
    """Poll the UPS once. Never raises; failures return reachable=False."""
    state = UpsState(last_poll=datetime.now(timezone.utc))

    if not cfg.configured:
        state.error = "SNMP not configured"
        return state
    if _privacy_unavailable(cfg):
        # Fail fast with the real reason; reachable stays False, so the fail-safe
        # behaviour (alarm, never a shutdown) is unchanged.
        state.error = PRIVACY_MISSING
        return state

    engine = None
    try:
        from pysnmp.hlapi.asyncio import (
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
        )

        # The GET command is called `getCmd` in pysnmp 6.x and `get_cmd` from 7.x on
        # (same signature and return value). Support both.
        try:
            from pysnmp.hlapi.asyncio import getCmd  # pysnmp 6.x
        except ImportError:
            from pysnmp.hlapi.asyncio import get_cmd as getCmd  # pysnmp 7.x

        engine = SnmpEngine()
        auth = _auth_data(cfg)
        transport = await _make_transport(cfg)

        async def get(oids: list[str]):
            objects = [ObjectType(ObjectIdentity(oid)) for oid in oids]
            return await getCmd(engine, auth, transport, ContextData(), *objects)

        async def get_each(oids: list[str]):
            """The same answer as get(), collected one object per request.

            A refused object (v1 noSuchName, or any other error status for that one
            object) becomes the v2c "missing" sentinel, so the rest of poll() and
            _map_state() treat it as they treat every missing object. A transport error
            ends the poll at once, exactly like a failed get(): a dead device still costs
            one timeout, not one per object.
            """
            from pysnmp.proto import rfc1905

            collected = []
            for oid in oids:
                error_indication, error_status, _, var_binds = await get([oid])
                if error_indication:
                    return error_indication, 0, 0, []
                if error_status or not var_binds:
                    collected.append((oid, rfc1905.noSuchObject))
                else:
                    collected.extend(var_binds)
            return None, 0, 0, collected

        def read(p: MibProfile):
            return get_each(p.oids) if p.single_varbind else get(p.oids)

        candidates = _profiles_for(cfg)
        profile = candidates[0]
        auto = len(candidates) > 1

        # "auto" over v2c/v3: append each vendor MIB's anchor to the standard GET. A
        # missing object comes back as a per-varbind sentinel there, so this costs one
        # round trip and tells us what the device actually implements. Not under SNMPv1 —
        # v1 aborts the whole GET over a single missing object, which would break every
        # non-vendor device; that case falls back on the error path below instead.
        vendors = candidates[1:] if auto and cfg.version != SnmpVersion.v1 else []
        oids = profile.oids + [v.anchor for v in vendors]

        # Without vendor anchors to append, this is exactly the profile's own read — which
        # for a single_varbind profile must not go out as one multi-object GET.
        error_indication, error_status, error_index, var_binds = await (
            get(oids) if vendors else read(profile)
        )

        if error_indication:
            # Transport level (timeout, wrong v3 user, DNS): the device is unreachable,
            # not speaking a different MIB. Never spend a second timeout on that.
            state.error = str(error_indication)
            return state
        # Past the transport level: whatever follows, this agent replied. Recorded here as
        # well as in _map_state(), because the error_status branch below returns without
        # reaching it — and a refused GET is still an answer, not silence.
        state.answered = True
        if error_status:
            refusal = f"{error_status.prettyPrint()} at index {error_index}"
            if not auto:
                state.error = refusal
                return state
            if vendors and (_coerce_int(error_index) or 0) > len(profile.oids):
                # Refused over one of the vendor anchors appended above. A conforming v2c
                # agent never does that — a missing object is a per-varbind sentinel there
                # — but real cards do: a CyberPower card answers the missing APC anchor
                # with v1's noSuchName, which used to make "auto" fail on every poll of a
                # UPS that is perfectly readable. Ask each anchor on its own instead: one
                # round trip per vendor, and only from an agent that just answered.
                found = []
                for vendor in vendors:
                    error_indication, error_status, _, var_binds = await get([vendor.anchor])
                    if error_indication:
                        state.error = str(error_indication)
                        return state
                    if not error_status and var_binds and _usable(var_binds[0][1]):
                        found.append(vendor)
                # Same rule as the regular path: the last vendor whose anchor answers wins.
                target = found[-1] if found else profile
                error_indication, error_status, error_index, var_binds = await read(target)
                if error_indication:
                    state.error = str(error_indication)
                    return state
                if error_status:
                    state.error = f"{error_status.prettyPrint()} at index {error_index}"
                    return state
                profile = target
                vendors = []  # resolved: the anchor pass below has nothing left to do
            else:
                # The standard GET was refused because one of its own objects is missing,
                # which is how SNMPv1 answers it on a card without RFC 1628 (an APC NMC1),
                # and how a non-conforming v2c agent does. Try the vendor MIBs. A profile
                # read one object per request is never refused as a whole, so it only
                # counts when at least one of its objects actually answered.
                for vendor in candidates[1:]:
                    error_indication, error_status, error_index, var_binds = await read(vendor)
                    if error_indication:
                        state.error = str(error_indication)
                        return state
                    if not error_status and any(_usable(v) for _, v in var_binds):
                        profile = vendor
                        break
                else:
                    state.error = refusal
                    return state
                vendors = []  # resolved, as above

        values: dict[str, object] = {}
        for var_bind in var_binds:
            oid, val = var_bind
            values[str(oid)] = val

        # A vendor anchor answered: the device speaks that MIB, so read it there.
        for vendor in vendors:
            if not _usable(values.get(vendor.anchor)):
                continue
            error_indication, error_status, error_index, var_binds = await read(vendor)
            if error_indication:
                state.error = str(error_indication)
                return state
            if error_status:
                state.error = f"{error_status.prettyPrint()} at index {error_index}"
                return state
            profile = vendor
            values = {str(oid): val for oid, val in var_binds}

        _map_state(state, profile, values, auto=auto)
        return state

    except Exception as exc:  # noqa: BLE001 - poller must never crash the loop
        log.warning("SNMP poll failed: %s", exc)
        state.error = str(exc)
        return state

    finally:
        # Always release the engine's socket/dispatcher — especially on the common
        # timeout/unreachable path, which would otherwise leak fastest.
        _close_engine(engine)


def _probe_entry(oid, error_indication, error_status, error_index, var_binds) -> ProbeEntry:
    """Classify the response of one single-OID GET. Never raises."""
    entry = ProbeEntry(oid=oid, name=_object_name(oid), status="error")
    try:
        if error_indication:
            # Transport level: timeout, unknown v3 user, wrong auth key, DNS failure.
            entry.error = str(error_indication)
            return entry
        if error_status:
            # noSuchName (errorStatus 2) is how SNMPv1 says "no such object here" —
            # the v1 counterpart of the v2c sentinels handled below.
            if _coerce_int(error_status) == 2:
                entry.status = "noSuchName"
            else:
                entry.error = f"{error_status.prettyPrint()} at index {error_index}"
            return entry
        if not var_binds:
            entry.error = "empty response"
            return entry

        _, value = var_binds[0]
        missing = _MISSING_SENTINELS.get(type(value).__name__)
        if missing:
            entry.status = missing
            return entry
        entry.status = "ok"
        entry.raw = _pretty(value)
        entry.value = _interpret(oid, value)
        return entry
    except Exception as exc:  # noqa: BLE001 - an odd ASN.1 type must not kill the test
        entry.status = "error"
        entry.error = str(exc)
        return entry


def _probe_summary(
    cfg: SnmpConfig, result: ProbeResult, first_error: Optional[str], profile: MibProfile
) -> str:
    """Short English overall diagnosis. Must never contain community or v3 secrets.

    Only the resolved profile's objects are judged. In "auto" mode the other MIB's entries
    are listed for diagnosis, but complaining that a device "lacks upsOutputSource" while
    happily reading it on the vendor MIB would be noise, not information.
    """
    where = f"{cfg.host}:{cfg.port} (SNMP {cfg.version.value})"
    if result.ok_count == 0:
        reason = first_error or "no object answered"
        return (
            f"No usable SNMP response from {where}: {reason}. Check address and port, the "
            f"SNMP credentials, and any firewall on UDP {cfg.port}."
        )

    own = {o.oid for o in profile.objects}
    mine = [e for e in result.entries if e.oid in own]
    missing = [e.name for e in mine
               if e.status in ("noSuchObject", "noSuchInstance", "noSuchName", "endOfMibView")]
    failed = [e.name for e in mine if e.status == "error"]

    if not missing and not failed:
        parts = [f"{profile.label}: all {result.total} objects answered on {where}."]
    else:
        parts = [f"{profile.label}: {result.ok_count} of {result.total} objects answered "
                 f"on {where}."]
        if missing:
            parts.append("Not provided by this UPS: " + ", ".join(missing) + ".")
            if any(e.status == "noSuchName" for e in mine):
                parts.append(
                    "SNMPv1 aborts a multi-object GET as soon as one object is missing, so "
                    "the regular poll fails entirely — use SNMP v2c if the UPS supports it."
                )
        if failed:
            parts.append("Failed: " + ", ".join(failed) + ".")

    # Say why a vendor MIB was chosen over the standard — otherwise "auto" is a black box.
    if _selected_mib(cfg) == SnmpMib.auto and profile is not DEFAULT_PROFILE:
        standard_answered = any(
            e.oid == DEFAULT_PROFILE.anchor and e.status == "ok" for e in result.entries
        )
        parts.append(
            f"This device also answers {DEFAULT_PROFILE.label}, but the {profile.label} MIB "
            "is preferred: it is the vendor's own and usually tells more apart (an APC "
            "self-test from an outage, for one)."
            if standard_answered else
            f"This device does not implement {DEFAULT_PROFILE.label}, so it is read via the "
            f"{profile.label} MIB."
        )
    return " ".join(parts)


def batching_hint(cfg: SnmpConfig, state: UpsState, result: ProbeResult) -> Optional[str]:
    """Explain a poll that got no answer while the per-object probe did (#40).

    The test button runs both, so this is the one place the pattern is visible: an agent
    that times out on a GET carrying several objects, yet answers each of them on its own.
    In "auto" mode the first GET always carries several (the standard profile plus the
    vendor anchors), so such a card can only work with its MIB pinned — and only a MIB
    that is read one object at a time. Nothing in the regular poll retries on its own:
    a second attempt after every timeout would double what a dead UPS costs.
    """
    if state.answered or not result.reachable:
        return None
    profile = PROFILES.get(result.mib)
    if profile is None:
        return None
    if profile.single_varbind:
        if _selected_mib(cfg) != SnmpMib.auto:
            return None  # already read one object at a time: the timeout is something else
        return (
            "The regular poll got no answer, yet the objects answer when asked one at a "
            f"time: this card does not handle a request for several objects at once. Set "
            f"this UPS's MIB to '{profile.label}' explicitly — that MIB is then read one "
            "object per request."
        )
    return (
        "The regular poll got no answer, yet the objects answer when asked one at a time. "
        "If this repeats, the card cannot handle a request for several objects at once, "
        f"which reading {profile.label} requires — please report the model in the "
        "project's issue tracker."
    )


async def probe(cfg: SnmpConfig) -> ProbeResult:
    """Query every object of every candidate MIB individually — manual test button only.

    poll() sends a whole profile in one GET: fast, but a single object the device does not
    implement makes the whole PDU fail under SNMPv1, so a user cannot tell a wrong
    community from a UPS that simply lacks upsSecondsOnBattery. One GET per object answers
    exactly that question. In "auto" mode every MIB is walked, which is what turns
    "nothing works" into "your card has no RFC 1628, but PowerNet answers everything".
    Never raises; the poll loop keeps using poll() unchanged.
    """
    candidates = _profiles_for(cfg)
    probe_oids = [oid for p in candidates for oid in p.oids]
    result = ProbeResult(total=len(candidates[0].objects), mib=candidates[0].id)

    def _skipped(oid: str) -> ProbeEntry:
        return ProbeEntry(oid=oid, name=_object_name(oid), status="skipped")

    # Nothing can be sent on either early-out, so report the profile that would have been
    # asked first rather than pretending every MIB was tried.
    if not cfg.configured:
        result.entries = [_skipped(oid) for oid in candidates[0].oids]
        result.summary = "SNMP not configured (no host set)."
        return result
    if _privacy_unavailable(cfg):
        # Per-OID results would be identical local errors and the usual "check the
        # firewall" advice would be actively misleading.
        result.entries = [_skipped(oid) for oid in candidates[0].oids]
        result.summary = PRIVACY_MISSING
        return result

    engine = None
    first_error: Optional[str] = None
    try:
        from pysnmp.hlapi.asyncio import (
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
        )

        # Same 6.x/7.x naming dance as poll().
        try:
            from pysnmp.hlapi.asyncio import getCmd  # pysnmp 6.x
        except ImportError:
            from pysnmp.hlapi.asyncio import get_cmd as getCmd  # pysnmp 7.x

        engine = SnmpEngine()
        auth = _auth_data(cfg)
        transport = await _make_transport(cfg)

        for oid in probe_oids:
            response = await getCmd(
                engine, auth, transport, ContextData(), ObjectType(ObjectIdentity(oid))
            )
            entry = _probe_entry(oid, *response)
            result.entries.append(entry)
            if entry.status == "error":
                if first_error is None:
                    first_error = entry.error
                # Give up while nothing has answered at all: a timeout per object costs
                # timeout x (retries+1) each — 42 s for seven with the defaults — and the
                # user is waiting in front of the test button. The remaining objects stay
                # "skipped", which says the same thing without the wait.
                if not any(e.status == "ok" for e in result.entries):
                    break

    except Exception as exc:  # noqa: BLE001 - the test button must never 500
        log.warning("SNMP probe failed: %s", exc)
        if first_error is None:
            first_error = str(exc)

    finally:
        _close_engine(engine)

    probed = {e.oid for e in result.entries}
    result.entries.extend(_skipped(oid) for oid in probe_oids if oid not in probed)
    answered = {e.oid for e in result.entries if e.status == "ok"}

    # Same rule as poll(): the last profile whose anchor answered wins, so a device that
    # implements both is read on the more precise vendor MIB.
    anchored = [c for c in candidates if c.anchor in answered]
    if anchored:
        profile = anchored[-1]
    else:
        # Nothing identified the device — go with whichever candidate answered most, which
        # is the standard on a tie because it is listed first.
        profile = max(candidates, key=lambda c: sum(1 for o in c.objects if o.oid in answered))

    own = {o.oid for o in profile.objects}
    result.mib = profile.id
    result.total = len(profile.objects)
    result.ok_count = sum(1 for e in result.entries if e.status == "ok" and e.oid in own)
    result.reachable = bool(answered)
    if result.reachable:
        result.missing_triggers = [
            trigger for oid, trigger in profile.trigger_oids.items() if oid not in answered
        ]
    # Stable sort: the resolved MIB's objects first, each block still in query order.
    result.entries.sort(key=lambda e: 0 if e.oid in own else 1)
    result.summary = _probe_summary(cfg, result, first_error, profile)
    return result
