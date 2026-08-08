param(
    [string]$BasePath = "$HOME\CodexProfiles",
    [string]$CodexCommand = "codex"
)

$ErrorActionPreference = "Stop"

function Initialize-CodexProfile {
    param(
        [Parameter(Mandatory = $true)][string]$Name
    )

    $profilePath = Join-Path $BasePath $Name
    New-Item -ItemType Directory -Force -Path $profilePath | Out-Null

    $configPath = Join-Path $profilePath "config.toml"
    @"
cli_auth_credentials_store = "file"
"@ | Set-Content -Path $configPath -Encoding UTF8

    Write-Host ""
    Write-Host "Login for profile: $Name" -ForegroundColor Cyan
    Write-Host "Use the correct ChatGPT account in the browser." -ForegroundColor Yellow

    $previous = $env:CODEX_HOME
    try {
        $env:CODEX_HOME = $profilePath
        & $CodexCommand login
        if ($LASTEXITCODE -ne 0) {
            throw "Codex login failed for profile '$Name'."
        }
        & $CodexCommand login status
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
