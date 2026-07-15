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
