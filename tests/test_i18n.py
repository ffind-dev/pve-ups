"""UI i18n consistency checks (regex-based, no JS engine needed).

Enforces: en.js and de.js define identical key sets, no duplicate keys, and every
key referenced from index.html (data-i18n* attributes) or app.js (literal t() calls)
exists in the English dictionary. Dynamic keys built at runtime ("theme." + mode,
"state.engine." + s, ...) are covered by the key-set checks on the dictionaries.
"""

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "app" / "web"

# One dictionary entry per line:   "some.key": "..." / function
KEY_RE = re.compile(r'^\s*"([^"]+)":', re.MULTILINE)
# Literal t("key") / t('key') calls in app.js (not preceded by an identifier char).
T_CALL_RE = re.compile(r"""(?<![A-Za-z0-9_$])t\(\s*["']([^"']+)["']""")
# data-i18n, data-i18n-html, data-i18n-title, data-i18n-placeholder in index.html.
ATTR_RE = re.compile(r'data-i18n(?:-[a-z]+)*="([^"]+)"')


def dict_keys(fname: str) -> list[str]:
    return KEY_RE.findall((WEB / "i18n" / fname).read_text(encoding="utf-8"))


def test_dictionaries_have_no_duplicate_keys():
    for fname in ("en.js", "de.js"):
        keys = dict_keys(fname)
        dupes = {k for k in keys if keys.count(k) > 1}
        assert not dupes, f"{fname}: duplicate keys {sorted(dupes)}"


def test_dictionary_key_sets_are_identical():
    en, de = set(dict_keys("en.js")), set(dict_keys("de.js"))
    assert en == de, (
        f"missing in de.js: {sorted(en - de)}; missing in en.js: {sorted(de - en)}"
    )


def test_index_html_keys_exist_in_english_dictionary():
    en = set(dict_keys("en.js"))
    used = set(ATTR_RE.findall((WEB / "index.html").read_text(encoding="utf-8")))
    assert used, "no data-i18n* attributes found in index.html"
    assert used <= en, f"index.html references unknown keys: {sorted(used - en)}"


def test_placeholders_match_between_languages():
    # {name} placeholders must be the same set per key, or t() interpolation breaks
    # silently in one language.
    line_re = re.compile(r'^\s*"([^"]+)":(.*)$', re.MULTILINE)
    ph_re = re.compile(r"\{([a-z]+)\}")

    def placeholders(fname):
        text = (WEB / "i18n" / fname).read_text(encoding="utf-8")
        return {k: set(ph_re.findall(rest)) for k, rest in line_re.findall(text)}

    en, de = placeholders("en.js"), placeholders("de.js")
    for key in en.keys() & de.keys():
        assert en[key] == de[key], (
            f"{key}: placeholders differ (en={sorted(en[key])}, de={sorted(de[key])})"
        )


def test_app_js_keys_exist_in_english_dictionary():
    en = set(dict_keys("en.js"))
    src = (WEB / "app.js").read_text(encoding="utf-8")
    used = {k for k in T_CALL_RE.findall(src) if not k.endswith(".")}
    assert used, "no t() calls found in app.js"
    assert used <= en, f"app.js references unknown keys: {sorted(used - en)}"


def _both_dictionaries_define(prefix: str, names) -> list[str]:
    en, de = set(dict_keys("en.js")), set(dict_keys("de.js"))
    return [f"{prefix}{n}" for n in names if f"{prefix}{n}" not in en or f"{prefix}{n}" not in de]


def test_probe_status_keys_exist_in_both_dictionaries():
    """Every per-object probe status the backend can emit needs a UI label in EN and DE."""
    from app.ups import PROBE_STATUSES

    missing = _both_dictionaries_define("probe.st.", PROBE_STATUSES)
    assert not missing, f"probe statuses without a dictionary entry: {missing}"


def test_cluster_probe_status_keys_exist_in_both_dictionaries():
    """Every per-endpoint outcome of the cluster check needs a UI label in EN and DE."""
    from app.cluster import CLUSTER_PROBE_STATUSES

    missing = _both_dictionaries_define("cprobe.st.", CLUSTER_PROBE_STATUSES)
    assert not missing, f"cluster probe statuses without a dictionary entry: {missing}"


def test_node_state_keys_exist_in_both_dictionaries():
    """Every node-name verdict needs a UI label in EN and DE.

    The dashboard shows a chip for the three states that mean "does not match this API";
    the other two exist so the closed set stays labelled, exactly like the probe statuses.
    """
    from app.proxmox import NODE_STATES

    missing = _both_dictionaries_define("nodest.", NODE_STATES)
    assert not missing, f"node states without a dictionary entry: {missing}"


def test_probe_trigger_keys_exist_in_both_dictionaries():
    """Same for the trigger names a probe reports as unavailable on this device."""
    from app.ups import PROBE_TRIGGERS

    missing = _both_dictionaries_define("probe.trg.", PROBE_TRIGGERS)
    assert not missing, f"probe triggers without a dictionary entry: {missing}"


def test_source_type_keys_exist_in_both_dictionaries():
    """Every UPS source type needs a label in EN and DE (wizard dropdown + dashboard)."""
    from app.config import UPS_SOURCE_MODELS, UpsSourceType

    kinds = [t.value for t in UpsSourceType]
    assert sorted(kinds) == sorted(UPS_SOURCE_MODELS), "UpsSourceType and the model map drifted apart"
    missing = _both_dictionaries_define("src.", kinds)
    assert not missing, f"source types without a dictionary entry: {missing}"

    # The wizard dropdown is built in JS; a type missing there is unreachable in the UI.
    block = re.search(r"const SOURCE_TYPES = \[(.*?)\];", (WEB / "app.js").read_text(encoding="utf-8"))
    assert block, "SOURCE_TYPES not found in app.js"
    assert re.findall(r'\["([a-z]+)"', block.group(1)) == kinds


def test_manual_deep_links_resolve_in_both_manuals():
    """Every anchor the UI deep-links to must exist in manual.html *and* handbuch.html.

    The links are built from data-manual attributes (static markup and, for the per-type
    host hint, from app.js), so a renamed section would silently land readers at the top
    of the document in one language only.
    """
    anchors = set()
    for name in ("index.html", "app.js"):
        anchors |= set(re.findall(r'data-manual="([a-z0-9-]+)"', (WEB / name).read_text(encoding="utf-8")))
    # app.js also assigns the attribute at runtime: doc.dataset.manual = … ? "a" : "b"
    js = (WEB / "app.js").read_text(encoding="utf-8")
    for line in re.findall(r"dataset\.manual\s*=\s*(.+)", js):
        anchors |= set(re.findall(r'"([a-z0-9-]+)"', line))
    assert "token-pbs" in anchors and "token-pve" in anchors, "host hint links not detected"

    for manual in ("manual.html", "handbuch.html"):
        have = set(re.findall(r'id="([a-z0-9-]+)"', (WEB / manual).read_text(encoding="utf-8")))
        missing = sorted(a for a in anchors if a not in have)
        assert not missing, f"{manual} has no section for: {missing}"


def test_host_type_keys_exist_in_both_dictionaries():
    """Every shutdown target type needs a label + a help line in EN and DE."""
    from app.config import TARGET_MODELS, HostType

    kinds = [t.value for t in HostType]
    assert sorted(kinds) == sorted(TARGET_MODELS), "HostType and the model map drifted apart"
    missing = _both_dictionaries_define("htype.", kinds)
    # Two more dynamic keys per type: the hint under the host form
    # (t("htype." + ty + "Help")) and the short label used where the full product name
    # would blow up the column (t("htype." + ty + "Short"), see hostTypeLabel).
    for suffix in ("Help", "Short"):
        missing += _both_dictionaries_define("htype.", [f"{k}{suffix}" for k in kinds])
    assert not missing, f"host types without a dictionary entry: {missing}"

    # The host dropdown is built in JS; a type missing there is unreachable in the UI.
    block = re.search(r"const HOST_TYPES = \[(.*?)\];", (WEB / "app.js").read_text(encoding="utf-8"))
    assert block, "HOST_TYPES not found in app.js"
    assert re.findall(r'\["([a-z]+)"', block.group(1)) == kinds


def test_snmp_mib_keys_exist_in_both_dictionaries():
    """Every selectable MIB needs a label in EN and DE (wizard dropdown + dashboard)."""
    from app.ups import PROFILES
    from app.config import SnmpMib

    kinds = [m.value for m in SnmpMib]
    # "auto" is a resolution mode, not a profile; every other member must have one.
    assert sorted(k for k in kinds if k != "auto") == sorted(PROFILES), \
        "SnmpMib and ups.PROFILES drifted apart"
    missing = _both_dictionaries_define("mib.", kinds)
    assert not missing, f"MIBs without a dictionary entry: {missing}"

    # The wizard dropdown is built in JS; a MIB missing there is unreachable in the UI.
    # re.S: the list is long enough to wrap in app.js.
    block = re.search(r"const SNMP_MIBS = \[(.*?)\];",
                      (WEB / "app.js").read_text(encoding="utf-8"), re.S)
    assert block, "SNMP_MIBS not found in app.js"
    # Digits allowed, unlike SOURCE_TYPES above: "rfc1628" carries its RFC number.
    assert re.findall(r'\["([a-z0-9_]+)"', block.group(1)) == kinds


def _select_values(select_id: str) -> list[str]:
    """The option values of a static <select> in index.html, in document order."""
    block = re.search(
        rf'<select id="{select_id}">(.*?)</select>',
        (WEB / "index.html").read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert block, f"{select_id} <select> not found in index.html"
    return re.findall(r'value="([a-z0-9_]+)"', block.group(1))


def _js_option_values(const_name: str) -> list[str]:
    """The values of a [["value", label], …] dropdown table in app.js, in order.

    Webhooks are rendered as cards (one per target), so their dropdowns are built in JS
    like SNMP_MIBS above rather than sitting in index.html as static markup.
    """
    block = re.search(
        rf"const {const_name} = \[(.*?)\];", (WEB / "app.js").read_text(encoding="utf-8")
    )
    assert block, f"{const_name} not found in app.js"
    return re.findall(r'\["([a-z0-9_]+)"', block.group(1))


def test_webhook_format_keys_exist_in_both_dictionaries():
    """Every payload format needs a renderer, a dropdown entry and labels in EN and DE."""
    from app.config import WebhookFormat
    from app.notify import FORMATTERS

    kinds = [f.value for f in WebhookFormat]
    assert sorted(kinds) == sorted(FORMATTERS), "WebhookFormat and notify.FORMATTERS drifted apart"
    # Label plus the help text shown below the dropdown (app.js builds that key dynamically).
    missing = _both_dictionaries_define("whfmt.", kinds) + _both_dictionaries_define(
        "whfmt.", [f"{k}Help" for k in kinds]
    )
    assert not missing, f"formats without a dictionary entry: {missing}"
    assert _js_option_values("WEBHOOK_FORMATS") == kinds


def test_webhook_level_keys_exist_in_both_dictionaries():
    """The severity filter must offer exactly the severities the event log uses."""
    from app import db
    from app.config import WebhookLevel

    kinds = [lv.value for lv in WebhookLevel]
    assert kinds == [db.INFO, db.WARNING, db.CRITICAL], "WebhookLevel and db severities drifted apart"
    missing = _both_dictionaries_define("whlvl.", kinds)
    assert not missing, f"levels without a dictionary entry: {missing}"
    assert _js_option_values("WEBHOOK_LEVELS") == kinds


def test_selftest_interval_options_match_the_backend():
    """The <select> in index.html must offer exactly config.SELFTEST_INTERVALS.

    HTML and Python constant are the one place where the selectable intervals can drift
    apart; a value the backend does not accept would be snapped back to 1440 on save.
    """
    from app.config import SELFTEST_INTERVALS

    block = re.search(
        r'<select id="selftest_interval_min">(.*?)</select>',
        (WEB / "index.html").read_text(encoding="utf-8"),
        re.DOTALL,
    )
    assert block, "selftest_interval_min <select> not found in index.html"
    values = [int(v) for v in re.findall(r'value="(\d+)"', block.group(1))]
    assert values == list(SELFTEST_INTERVALS)
