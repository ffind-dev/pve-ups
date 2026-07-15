#Requires -Version 7
<#
.SYNOPSIS
  Builds the deploy artifact of the PVE-UPS appliance (Windows/PowerShell variant of
  make-dist.sh). Identical result: a reproducible tarball with a uniform naming/layout
  scheme that contains only the files needed to install and operate.

  Everything that must stay out (docs/, tests/, snmpdata/, scripts/, git metadata,
  READMEs) is marked in .gitattributes via `export-ignore`; `git archive` omits `.git`
  itself anyway.

  Output format (uniform):
    dist/pve-usv-<version>.tar.gz   with the internal prefix folder  pve-usv/

.PARAMETER Committed
  If set, the committed state ($Ref, default HEAD) is archived instead of the working
  copy. Default without the switch: the WORKING COPY (incl. uncommitted changes) — so
  the filename version matches the content version.

.PARAMETER Ref
  Only relevant with -Committed: the git state to archive (default HEAD).

.EXAMPLE
  scripts\make-dist.ps1
.EXAMPLE
  scripts\make-dist.ps1 -Committed v3.0.0
#>
[CmdletBinding()]
param([switch]$Committed, [string]$Ref = 'HEAD')

$ErrorActionPreference = 'Stop'

$repoRoot = (git rev-parse --show-toplevel).Trim()
Set-Location $repoRoot

# Single source of the version: app/__init__.py.
$m = Select-String -Path 'app/__init__.py' -Pattern '^__version__ = "(.*)"'
if (-not $m) { throw "Could not read __version__ from app/__init__.py." }
$version = $m.Matches[0].Groups[1].Value

$prefix = 'pve-usv/'
$out    = "dist/pve-usv-$version.tar.gz"

# Which tree to archive? Default: the complete working copy. For that we write a
# tree object from a TEMPORARY index (the real index stays untouched): `git add -A`
# captures tracked changes AND untracked files (e.g. new deploy/* units) and
# respects .gitignore. `git stash create` would miss untracked files.
if ($Committed) {
  $tree = $Ref
  Write-Host ">> Building from committed state: $Ref"
} else {
  $tmpIndex = (New-TemporaryFile).FullName
  try {
    $env:GIT_INDEX_FILE = $tmpIndex
    git read-tree HEAD
    git add -A                         # tracked + untracked, .gitignore-aware
    # Drop tracked files that are ignore-listed (incl. .git/info/exclude): local-only
    # files must never end up in the artifact, even while they are still tracked.
    $ignored = (git ls-files -z --cached --ignored --exclude-standard) -split "`0" |
      Where-Object { $_ }
    if ($ignored) { git update-index --force-remove -- @($ignored) }
    $tree = (git write-tree).Trim()
  } finally {
    Remove-Item env:GIT_INDEX_FILE -ErrorAction SilentlyContinue
    Remove-Item $tmpIndex -Force -ErrorAction SilentlyContinue
  }
  Write-Host ">> Note: packaging the working copy (incl. untracked files)"
  Write-Host "   (version $version == content). For the committed state: -Committed."
}

New-Item -ItemType Directory -Force -Path 'dist' | Out-Null
git archive --format=tar.gz --prefix=$prefix -o $out $tree
if ($LASTEXITCODE -ne 0) { throw "git archive failed (tree: $tree)." }

# Safety net: verify the actual version INSIDE the archive against the filename.
$pkgInit = (tar -xzO -f $out "${prefix}app/__init__.py") -join "`n"
$pm = [regex]::Match($pkgInit, '^__version__ = "(.*)"', 'Multiline')
$pkgVersion = if ($pm.Success) { $pm.Groups[1].Value } else { '' }
if ($pkgVersion -ne $version) {
  throw "Version in the package ('$pkgVersion') != filename ('$version'). With -Committed, HEAD may contain a different version."
}

# Stable alias for the one-liner bootstrap (install.sh fetches
# pve-usv-latest.tar.gz by default). Upload install.sh separately as well.
Copy-Item $out 'dist/pve-usv-latest.tar.gz' -Force

Write-Host ">> Created: $out  (package version $pkgVersion, + pve-usv-latest.tar.gz)"
Write-Host ">> Contents:"
tar -tzf $out | ForEach-Object { "   $_" }
Write-Host ""
Write-Host ">> Release assets (the release workflow uploads these automatically on a tag):"
Write-Host "     pve-usv-$version.tar.gz, pve-usv-latest.tar.gz and install.sh"
Write-Host ">> One-liner in the Proxmox node shell (as root):"
Write-Host "     bash -c `"`$(curl -fsSL https://github.com/ffind-dev/pve-ups/releases/latest/download/install.sh)`""
