"""PVE-UPS — minimal UPS shutdown appliance for Proxmox VE.

Copyright 2026 Florian Finder
"""

# Single source of truth for the version (pyproject.toml reads this dynamically,
# and app.main exposes it via the REST API). Bump this on every release and add a
# matching CHANGELOG.md entry.
__version__ = "3.0.0"
