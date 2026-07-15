# Security Policy

## Supported versions

Only the **latest release** receives fixes. Please update to the current version before
reporting.

## Reporting a vulnerability

Please report vulnerabilities **privately** via GitHub's security advisory form:
[Report a vulnerability](https://github.com/ffind-dev/pve-ups/security/advisories/new) —
do not open a public issue. Include the affected version, a description and, if possible,
steps to reproduce.

## Scope notes

- The appliance is designed for **trusted management networks**. The read-only endpoints
  (`/api/status`, `/api/health`) are intentionally unauthenticated but never expose
  secrets; all modifying endpoints are password-protected.
- The configuration export contains credentials in plain text by design (for
  backup/migration) — handle exported files accordingly.
