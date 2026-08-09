[CmdletBinding()]
param(
    [string]$CodexHome = $env:CODEX_HOME
)

$ErrorActionPreference = "Stop"
$repoRoot = (Resolve-Path -LiteralPath $PSScriptRoot).Path
$source = Join-Path $repoRoot "skills\bigqmt-history-export"

if ([string]::IsNullOrWhiteSpace($CodexHome)) {
    $CodexHome = Join-Path $HOME ".codex"
}

$skillsRoot = Join-Path $CodexHome "skills"
$destination = Join-Path $skillsRoot "bigqmt-history-export"
New-Item -ItemType Directory -Path $skillsRoot -Force | Out-Null
New-Item -ItemType Directory -Path $destination -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $source "SKILL.md") -Destination $destination -Force
Copy-Item -LiteralPath (Join-Path $source "agents") -Destination $destination -Recurse -Force
Copy-Item -LiteralPath (Join-Path $source "references") -Destination $destination -Recurse -Force
Copy-Item -LiteralPath (Join-Path $source "scripts") -Destination $destination -Recurse -Force

Write-Output "Installed bigqmt-history-export to $destination"
