<#
.SYNOPSIS
  Runs the CAIPE supervisor (single-node mode) locally on Windows for
  end-to-end testing of the autonomous_agents service against a live
  supervisor — without requiring Docker, MongoDB, or any other external
  service.

.DESCRIPTION
  This script is a development helper for the autonomous_agents feature.
  It does NOT modify any tracked file in the repo — all workarounds
  needed to run the supervisor natively on Windows live here:

    1. Sets PYTHONUTF8=1 / PYTHONIOENCODING=utf-8
       Required because:
         - prompts.py opens prompt_config.yaml without an encoding= arg,
           which falls back to Windows cp1252 and chokes on emoji
           characters (🔍, ✅, ☐) present in the prompt template.
         - The supervisor's connectivity table prints box-drawing
           characters that cp1252 cannot encode to stdout.
       PYTHONUTF8=1 fixes both at the interpreter level — no source
       patches needed.

    2. Runs the supervisor with cwd = charts/ai-platform-engineering/data
       The supervisor's prompts.py loads prompt_config.yaml via a
       *relative* path. The repo-root prompt_config.yaml is intentionally
       a single-line stub used as a Docker volume-mount target. Running
       from charts/.../data resolves the relative path to the real
       config without touching any tracked file.

    3. Bootstraps a .pth file inside the venv that exposes all sibling
       agent_<name> packages on sys.path
       The single-mode supervisor hard-imports agent_github,
       agent_argocd, etc. These are sibling sub-packages with their own
       pyproject.toml that are NOT installed by `uv sync` at the repo
       root. Writing a .pth file inside the venv site-packages is the
       least invasive way to expose them without modifying the root
       pyproject.toml or installing 11 separate editable packages.

  None of these workarounds change repo-tracked files. They are purely
  local dev-environment shims so the autonomous_agents service has a
  live supervisor to call into during development.

.PARAMETER Port
  Port to bind the supervisor to. Defaults to 8000.

.EXAMPLE
  pwsh ai_platform_engineering/autonomous_agents/scripts/run_supervisor_local.ps1
#>

[CmdletBinding()]
param(
  [int]$Port = 8000
)

$ErrorActionPreference = 'Stop'

# Resolve repo root from this script's location:
#   <repo>/ai_platform_engineering/autonomous_agents/scripts/<this>.ps1
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot '..\..\..')
$VenvPython = Join-Path $RepoRoot '.venv\Scripts\python.exe'
$SitePackages = Join-Path $RepoRoot '.venv\Lib\site-packages'
$PthFile = Join-Path $SitePackages 'caipe_agents.pth'
$PromptCfgDir = Join-Path $RepoRoot 'charts\ai-platform-engineering\data'

if (-not (Test-Path $VenvPython)) {
  throw "Repo-root venv not found at $VenvPython. Run 'uv sync' at the repo root first."
}
if (-not (Test-Path $PromptCfgDir)) {
  throw "Expected supervisor prompt config dir not found: $PromptCfgDir"
}

# 1. Bootstrap the agent_<name> .pth file if missing.
$AgentNames = @(
  'github','backstage','jira','webex','argocd','aigateway',
  'pagerduty','slack','splunk','komodor','confluence'
)
$AgentPaths = $AgentNames | ForEach-Object {
  Join-Path $RepoRoot ("ai_platform_engineering\agents\$_")
}
$existing = if (Test-Path $PthFile) { Get-Content $PthFile } else { @() }
$needsRewrite = $false
foreach ($p in $AgentPaths) {
  if ($existing -notcontains $p) { $needsRewrite = $true; break }
}
if ($needsRewrite) {
  Write-Host "[run_supervisor_local] Bootstrapping $PthFile (exposes 11 agent_* packages)"
  $AgentPaths | Set-Content -Path $PthFile -Encoding utf8
}

# 2. Force UTF-8 mode for the supervisor interpreter.
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# 3. Ensure the ai_platform_engineering package is importable. We're about
#    to cd away from the repo root, which removes it from the implicit
#    sys.path[0]; PYTHONPATH puts it back explicitly.
$env:PYTHONPATH = $RepoRoot

# 4. cd into the dir containing the real prompt_config.yaml so the
#    supervisor's relative-path lookup resolves correctly.
Push-Location $PromptCfgDir
try {
  Write-Host "[run_supervisor_local] cwd = $PromptCfgDir"
  Write-Host "[run_supervisor_local] launching supervisor on port $Port (Ctrl+C to stop)"
  # Note: --port is a Click group-level option, must come BEFORE the subcommand
  & $VenvPython -m ai_platform_engineering.multi_agents --port $Port platform-engineer-single
}
finally {
  Pop-Location
}
