#!/usr/bin/env bash
#
# Builds the deploy artifact of the PVE-UPS appliance: a reproducible tarball
# with a uniform naming/layout scheme that contains only the files needed to
# install and operate. Everything that must stay out (docs/, tests/, snmpdata/,
# scripts/, git metadata, READMEs) is marked in .gitattributes via
# `export-ignore`; `git archive` omits `.git` itself anyway.
#
# Output format (uniform):
#   dist/pve-usv-<version>.tar.gz   with the internal prefix folder  pve-usv/
#
# Usage:
#   scripts/make-dist.sh            # archives the WORKING COPY (incl. uncommitted
#                                   #   changes) -> filename version == content version
#   scripts/make-dist.sh --committed [<ref>]   # archives the committed state (HEAD/<ref>)
#
# Important (background): previously `git archive HEAD` was always built, but the
# filename was taken from the working copy. With uncommitted version bumps the
# package then contained the OLD code under a NEW name — the web updater reported
# "version unchanged". The default is therefore the working tree now.
#
# Note: `export-ignore` is read from the archived tree.
set -euo pipefail

COMMITTED=0
REF="HEAD"
for arg in "$@"; do
  case "$arg" in
    --committed) COMMITTED=1;;
    *) REF="$arg";;
  esac
done

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

# Single source of the version: app/__init__.py.
VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' app/__init__.py)"
[[ -n "$VERSION" ]] || { echo "Could not read __version__ from app/__init__.py."; exit 1; }

OUT_DIR="dist"
PREFIX="pve-usv/"
OUT="${OUT_DIR}/pve-usv-${VERSION}.tar.gz"

# Which tree to archive? Default: the complete working copy. For that we write a
# tree object from a TEMPORARY index (the real index stays untouched): `git add -A`
# captures tracked changes AND untracked files (e.g. new deploy/* units) and
# respects .gitignore. `git stash create` would miss untracked files.
if [[ "$COMMITTED" -eq 1 ]]; then
  TREE="$REF"
  echo ">> Building from committed state: ${REF}"
else
  TMP_INDEX="$(mktemp)"
  trap 'rm -f "$TMP_INDEX"' EXIT
  GIT_INDEX_FILE="$TMP_INDEX" git read-tree HEAD
  GIT_INDEX_FILE="$TMP_INDEX" git add -A          # tracked + untracked, .gitignore-aware
  # Drop tracked files that are ignore-listed (incl. .git/info/exclude): local-only
  # files must never end up in the artifact, even while they are still tracked.
  GIT_INDEX_FILE="$TMP_INDEX" git ls-files -z --cached --ignored --exclude-standard \
    | GIT_INDEX_FILE="$TMP_INDEX" git update-index -z --force-remove --stdin
  TREE="$(GIT_INDEX_FILE="$TMP_INDEX" git write-tree)"
  echo ">> Note: packaging the working copy (incl. untracked files)"
  echo "   (version ${VERSION} == content). For the committed state: --committed."
fi

mkdir -p "$OUT_DIR"
git archive --format=tar.gz --prefix="$PREFIX" -o "$OUT" "$TREE"

# Safety net: verify the actual version INSIDE the archive against the filename.
PKG_VERSION="$(tar -xzO -f "$OUT" "${PREFIX}app/__init__.py" | sed -n 's/^__version__ = "\(.*\)"/\1/p')"
if [[ "$PKG_VERSION" != "$VERSION" ]]; then
  echo "ERROR: version in the package ('${PKG_VERSION:-empty}') != filename ('${VERSION}')." >&2
  echo "       (With --committed, HEAD may contain a different version.)" >&2
  exit 1
fi

# Stable alias for the one-liner bootstrap (install.sh fetches
# pve-usv-latest.tar.gz by default). Upload install.sh separately as well.
cp "$OUT" "${OUT_DIR}/pve-usv-latest.tar.gz"

echo ">> Created: ${OUT}  (package version ${PKG_VERSION}, + pve-usv-latest.tar.gz)"
echo ">> Contents:"
tar -tzf "$OUT" | sed 's/^/   /'
echo ""
echo ">> Release assets (the release workflow uploads these automatically on a tag):"
echo "     pve-usv-${VERSION}.tar.gz, pve-usv-latest.tar.gz and install.sh"
echo ">> One-liner in the Proxmox node shell (as root):"
echo "     bash -c \"\$(curl -fsSL https://github.com/ffind-dev/pve-ups/releases/latest/download/install.sh)\""
