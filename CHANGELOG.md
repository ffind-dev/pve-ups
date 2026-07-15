# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versioning
follows [Semantic Versioning](https://semver.org/) (MAJOR.MINOR.PATCH).

The single source of the version is `app/__init__.py` (`__version__`); `pyproject.toml`
reads it dynamically. On every release: bump `__version__` **and** add a section here.

## [Unreleased]

## [3.0.0] - 2026-07-15

First public release, under the public name **PVE-UPS** (technical identifiers — package,
paths, systemd units, env vars — deliberately stay `pve-usv` so in-place updates from 2.x
keep working).

### Added
- Bilingual web UI: English (default) and German. The language is picked automatically
  from the browser language and is easy to extend
  (`app/web/i18n/<lang>.js` + one `<script>` tag).
- English user manual (`manual.html`) alongside the German one (`handbuch.html`), same
  structure and anchors; the ?-icon and all deep links open the manual matching the UI
  language.
- i18n consistency tests (`tests/test_i18n.py`): key parity between the dictionaries,
  placeholder parity, and referenced-key checks for `index.html`/`app.js`.
- Installation and updates via GitHub releases: install one-liner downloads
  `install.sh` from the latest release; the release tarball doubles as the update package
  for the web UI uploader.

### Changed
- **Breaking:** all backend texts — event log entries, trigger reasons, webhook messages,
  API error details — are now uniformly English, regardless of the UI language.
- Webhook subject prefix is now `[PVE-UPS]`.
- Visible product name is PVE-UPS (UI, documentation, webhook); internal names stay
  `pve-usv`.

### Fixed
- The UPS card and the outage banner no longer show the time-based countdown once a UPS
  has triggered (battery low, runtime or charge threshold) — those conditions fire
  immediately and the ticking countdown wrongly suggested the shutdown would wait for
  it. The banner now also explains when triggered UPS are waiting for a host's
  AND/OR policy.

### Removed
- **Breaking:** e-mail (SMTP) notifications. The webhook remains and covers notification
  needs; a legacy `notifications.smtp` config key is ignored on load and dropped on the
  next save.

[Unreleased]: https://github.com/ffind-dev/pve-ups/compare/v3.0.0...HEAD
[3.0.0]: https://github.com/ffind-dev/pve-ups/releases/tag/v3.0.0
