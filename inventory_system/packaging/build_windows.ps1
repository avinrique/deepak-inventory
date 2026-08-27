<#
.SYNOPSIS
    Builds the Windows executable and installer for Inventory Management System.

.DESCRIPTION
    Run this from a PowerShell prompt on a 64-bit Windows machine with
    Python 3.12 installed:

        cd inventory_system
        .\packaging\build_windows.ps1

    It produces:
        dist\InventoryManagementSystem\InventoryManagementSystem.exe
        dist\installer\InventoryManagementSystemSetup-<version>.exe

    The same steps run in .github/workflows/windows-build.yml, so a local
    build and a CI build stay in step. CI is the reference; this exists for
    fast iteration and for testing on real hardware.

.PARAMETER SkipTests
    Skip the test suite. For quick iteration only — CI always runs it.

.PARAMETER SkipInstaller
    Build the .exe but not the installer (useful without Inno Setup).
#>
[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$SkipInstaller
)

$ErrorActionPreference = "Stop"

# Always operate from the project directory, whatever the caller's location:
# the .spec and the app both resolve paths relative to it.
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $ProjectRoot

function Write-Step($message) {
    Write-Host ""
    Write-Host "==> $message" -ForegroundColor Cyan
}

try {
    Write-Step "Checking Python"
    $pythonVersion = & python --version 2>&1
    Write-Host "    $pythonVersion"
    if ($pythonVersion -notmatch "3\.1[2-9]") {
        Write-Warning "Python 3.12+ is expected; the build may still work."
    }

    Write-Step "Installing dependencies"
    & python -m pip install --upgrade pip --quiet
    & python -m pip install -r requirements.txt -r requirements-dev.txt --quiet
    if ($LASTEXITCODE -ne 0) { throw "pip install failed" }

    if (-not $SkipTests) {
        Write-Step "Running tests"
        # offscreen so the UI tests actually run rather than skipping for
        # want of a display when this is invoked from a service or SSH.
        $env:QT_QPA_PLATFORM = "offscreen"
        & python -m pytest -q
        if ($LASTEXITCODE -ne 0) { throw "tests failed" }
        Remove-Item Env:\QT_QPA_PLATFORM
    }

    Write-Step "Generating version resource"
    & python packaging\make_version_info.py
    if ($LASTEXITCODE -ne 0) { throw "make_version_info.py failed" }

    Write-Step "Staging PostgreSQL backup tools"
    # --optional: a machine without PostgreSQL still produces a working
    # build, just one where Backup reports the tools are missing.
    & python packaging\fetch_pgtools.py --optional

    Write-Step "Building the executable"
    Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
    & python -m PyInstaller packaging\InventoryManagementSystem.spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

    Write-Step "Self-testing the packaged executable"
    # The point of the whole exercise: verify the built .exe, not the source
    # tree. Catches a stylesheet or hidden import left out of the .spec.
    $env:QT_QPA_PLATFORM = "offscreen"
    & dist\InventoryManagementSystem\InventoryManagementSystem.exe --self-test
    if ($LASTEXITCODE -ne 0) { throw "the packaged application failed its self-test" }
    Remove-Item Env:\QT_QPA_PLATFORM

    if (-not $SkipInstaller) {
        Write-Step "Building the installer"
        $iscc = @(
            "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
            "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
        ) | Where-Object { Test-Path $_ } | Select-Object -First 1

        if (-not $iscc) {
            Write-Warning "Inno Setup 6 not found - skipping the installer."
            Write-Warning "Install it from https://jrsoftware.org/isdl.php"
        } else {
            $version = (Select-String -Path app\__version__.py -Pattern '^VERSION\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
            & $iscc "/DAppVersion=$version" packaging\installer.iss
            if ($LASTEXITCODE -ne 0) { throw "Inno Setup failed" }
        }
    }

    Write-Step "Done"
    Get-ChildItem -Recurse dist\installer -Filter *.exe -ErrorAction SilentlyContinue |
        ForEach-Object { Write-Host "    Installer: $($_.FullName)" -ForegroundColor Green }
    Write-Host "    Application: $ProjectRoot\dist\InventoryManagementSystem\" -ForegroundColor Green
}
finally {
    Pop-Location
}
