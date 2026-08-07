param(
    [string]$BasePath = "$HOME\CodexProfiles",
    [string]$CodexCommand = "codex"
)

$ErrorActionPreference = "Stop"

if (-not (Get-Command $CodexCommand -ErrorAction SilentlyContinue)) {
    throw "Codex CLI was not found. Install it first, then verify with 'codex --version'."
}

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)

function Initialize-CodexProfile {
    param(
        [Parameter(Mandatory = $true)][string]$Name
    )

    $profilePath = Join-Path $BasePath $Name
    New-Item -ItemType Directory -Force -Path $profilePath | Out-Null

    $configPath = Join-Path $profilePath "config.toml"
    $configContent = "cli_auth_credentials_store = `"file`"`n"
    [System.IO.File]::WriteAllText($configPath, $configContent, $utf8NoBom)

    Write-Host ""
    Write-Host "Login for profile: $Name" -ForegroundColor Cyan
    Write-Host "Use the intended ChatGPT account in the browser." -ForegroundColor Yellow

    $previous = $env:CODEX_HOME
    try {
        $env:CODEX_HOME = $profilePath
        & $CodexCommand login
        if ($LASTEXITCODE -ne 0) {
            throw "Codex login failed for profile '$Name'."
        }
        & $CodexCommand login status
        if ($LASTEXITCODE -ne 0) {
            throw "Codex login status failed for profile '$Name'."
        }
    }
    finally {
        $env:CODEX_HOME = $previous
    }
}

New-Item -ItemType Directory -Force -Path $BasePath | Out-Null
Initialize-CodexProfile -Name "architect"
Initialize-CodexProfile -Name "executor"

Write-Host ""
Write-Host "Profiles created under $BasePath" -ForegroundColor Green
Write-Host "Do not commit or share auth.json files." -ForegroundColor Yellow
