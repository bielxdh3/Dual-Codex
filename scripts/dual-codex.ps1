param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Arguments
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$srcPath = Join-Path $projectRoot "src"

function Find-Python {
    if ($env:VIRTUAL_ENV) {
        $active = Join-Path $env:VIRTUAL_ENV "Scripts\python.exe"
        if (Test-Path -LiteralPath $active) { return $active }
    }
    $local = Join-Path $projectRoot ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $local) { return $local }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { return $python.Source }
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) { return $py.Source }
    throw "Python 3.11 or newer was not found. Activate a virtual environment or install Python."
}

$python = Find-Python
$previousPythonPath = $env:PYTHONPATH
if ($previousPythonPath) {
    $env:PYTHONPATH = "$srcPath$([IO.Path]::PathSeparator)$previousPythonPath"
} else {
    $env:PYTHONPATH = $srcPath
}

try {
    & $python -m dual_codex.cli @Arguments
    exit $LASTEXITCODE
} catch {
    Write-Error "Dual Codex launcher failed: $($_.Exception.Message)"
    exit 1
} finally {
    $env:PYTHONPATH = $previousPythonPath
}
