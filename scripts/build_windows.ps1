<#
Build the Windows desktop app (PyInstaller one-folder) and, with -Deploy, install it over the running copy.

    powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1            # build only -> dist\PhotoScanner
    powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1 -Deploy    # build, back up, swap, restart

Needs Python 3.12 (`py -3.12`; winget install Python.Python.3.12 --scope user). The build environment lives in
.venv312\ and is reused between runs.

Native core: this PC has no C++ toolchain, so the compiled core (_banana_core.cp312-win_amd64.pyd) is taken from
-NativeFrom, by default the installed app. That is only valid while native\ is unchanged since that file was
built; the native-vs-reference parity tests (tests\test_core.py) run against it and the build stops if they fail.
With no compiled core at all the app still works on the NumPy reference (identical results, ~0.3 s slower per
ingested photo): pass -AllowNoNative.

-Deploy, in order: refuses if a scan or an ingest looks active (-Force skips that); stops the app; backs up the
database (SQLite backup API + integrity check) and config.toml to %LOCALAPPDATA%\PhotoScanner\backups\; renames
the installed folder to PhotoScanner_prev_<time> for rollback; copies the new build to the same path (config.toml
points ExifTool there); starts it and waits for /health.
Rollback: stop the app, delete the install folder, rename PhotoScanner_prev_<time> back, and restore the
backed-up banana.db only if the new version changed something you need undone.
#>
param(
    [string]$InstallDir = (Join-Path $env:USERPROFILE "Downloads\PhotoScanner_win64\PhotoScanner"),
    [string]$NativeFrom = "",
    [switch]$Deploy,
    [switch]$Force,
    [switch]$AllowNoNative
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Venv = Join-Path $Repo ".venv312"
$Py = Join-Path $Venv "Scripts\python.exe"
$DataHome = Join-Path $env:LOCALAPPDATA "PhotoScanner"
$Port = 8420
$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"

function Step($text) { Write-Host "`n== $text" -ForegroundColor Cyan }
function Run($exe) {
    & $exe @args
    if ($LASTEXITCODE -ne 0) { throw "failed ($LASTEXITCODE): $exe $args" }
}

Set-Location $Repo

# ---------------------------------------------------------------- build environment
Step "Python 3.12 build environment ($Venv)"
& py -3.12 --version
if ($LASTEXITCODE -ne 0) { throw "Python 3.12 not found. Install it: winget install Python.Python.3.12 --scope user" }
if (-not (Test-Path $Py)) { Run py -3.12 -m venv $Venv }
Run $Py -m pip install --quiet --upgrade pip
Run $Py -m pip install --quiet --upgrade ".[ocr,ner]" pyinstaller pytest
# Dependencies only: the app's own code must come from this checkout (the spec's pathex), never from an installed
# copy. pip skips reinstalling it while the version number is unchanged, which would bundle stale code.
Run $Py -m pip uninstall --quiet --yes photo-scanner-banana
$from = (& $Py -c "import banana, pathlib; print(pathlib.Path(banana.__file__).resolve().parent.parent)").Trim()
if ($from -ne (Resolve-Path $Repo).Path) { throw "banana would be imported from $from, not this checkout" }

# ---------------------------------------------------------------- native core
Step "Native core"
$SitePkgs = (& $Py -c "import sysconfig; print(sysconfig.get_paths()['purelib'])").Trim()
$CoreDir = Join-Path $SitePkgs "banana_core"
if (-not $NativeFrom) { $NativeFrom = Join-Path $InstallDir "_internal\banana_core" }
$Pyd = Get-ChildItem -Path $NativeFrom -Filter "_banana_core.cp312-*.pyd" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($Pyd) {
    New-Item -ItemType Directory -Force $CoreDir | Out-Null
    Copy-Item (Join-Path $Repo "native\python\banana_core\__init__.py") $CoreDir -Force
    Copy-Item $Pyd.FullName $CoreDir -Force
    Write-Host "using $($Pyd.FullName)"
} elseif (-not (Test-Path (Join-Path $CoreDir "__init__.py"))) {
    if (-not $AllowNoNative) { throw "No compiled core found in $NativeFrom. Pass -NativeFrom <folder with _banana_core.cp312-*.pyd>, or -AllowNoNative." }
    Write-Warning "building WITHOUT the native core: the app uses the NumPy reference (slower per photo)"
}
$Native = (& $Py -c "from banana import core; print(core.NATIVE_AVAILABLE)").Trim()
Write-Host "native core available: $Native"
if ($Native -eq "True") {
    Run $Py -m pytest tests\test_core.py -q -p no:cacheprovider   # native must match the reference bit for bit
}

# ---------------------------------------------------------------- build
Step "PyInstaller build -> dist\PhotoScanner"
foreach ($dir in "build", "dist") { if (Test-Path $dir) { Remove-Item -Recurse -Force $dir } }
Run (Join-Path $Venv "Scripts\pyinstaller.exe") scripts\photoscanner.spec --noconfirm --log-level WARN --distpath dist --workpath build
$Built = Join-Path $Repo "dist\PhotoScanner"
if (-not (Test-Path (Join-Path $Built "PhotoScanner.exe"))) { throw "build produced no PhotoScanner.exe" }
if (-not (Test-Path (Join-Path $Built "_internal\tools\exiftool\exiftool.exe"))) { throw "build is missing ExifTool" }
Write-Host "built: $Built ($(git rev-parse --short HEAD))"
if (-not $Deploy) { Write-Host "`nBuild only. Run again with -Deploy to install it."; exit 0 }

# ---------------------------------------------------------------- deploy
Step "Checking the app is idle"
$Config = Join-Path $DataHome "config\config.toml"
$Inbox = $null
if (Test-Path $Config) {
    $m = Select-String -Path $Config -Pattern '^\s*inbox\s*=\s*"(.+)"' | Select-Object -First 1
    if ($m) { $Inbox = $m.Matches[0].Groups[1].Value }
}
if ($Inbox -and (Test-Path $Inbox) -and -not $Force) {
    $scanning = Get-ChildItem -Path $Inbox -Directory -Force -Filter ".scanning-*" -ErrorAction SilentlyContinue
    $fresh = Get-ChildItem -Path $Inbox -File -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTime -gt (Get-Date).AddMinutes(-2) }
    $waiting = Get-ChildItem -Path $Inbox -File -ErrorAction SilentlyContinue
    if ($scanning) { throw "a scan run is in progress or was cut off ($($scanning.Name -join ', ')). Finish or recover it first, or pass -Force." }
    if ($fresh -or $waiting) { throw "$(@($waiting).Count) file(s) are waiting in the inbox (ingest still running?). Wait for them, or pass -Force." }
}

Step "Stopping the app"
$procs = Get-Process -Name "PhotoScanner" -ErrorAction SilentlyContinue
if ($procs) {
    $procs | Stop-Process -Force
    $procs | Wait-Process -Timeout 30 -ErrorAction SilentlyContinue
}
Write-Host "stopped"

Step "Backing up the database and config"
$Backup = Join-Path $DataHome "backups\pre-update-$Stamp"
$Db = Join-Path $DataHome "data\banana.db"
if (Test-Path $Db) {
    Run $Py scripts\windows_update.py backup $Db (Join-Path $Backup "banana.db")
}
if (Test-Path $Config) { Copy-Item $Config $Backup -Force }

Step "Installing"
$Prev = $null
if (Test-Path $InstallDir) {
    $Prev = "$InstallDir`_prev_$Stamp"
    Rename-Item $InstallDir (Split-Path -Leaf $Prev)
    Write-Host "previous build kept at $Prev"
}
Copy-Item $Built $InstallDir -Recurse
Write-Host "installed to $InstallDir"

Step "Starting"
Start-Process (Join-Path $InstallDir "PhotoScanner.exe")
$health = $null
for ($i = 0; $i -lt 60 -and -not $health; $i++) {
    Start-Sleep -Seconds 1
    try { $health = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 2 } catch { }
}
if (-not $health) {
    throw "the new build did not answer on port $Port within 60 s. Logs: $DataHome\logs. Previous build: $Prev"
}
Write-Host "running: version $($health.version), native core $($health.native_core)"
if ($Native -eq "True" -and -not $health.native_core) { Write-Warning "built with the native core, but the running app reports it unavailable" }
Write-Host "`nDone. Backup: $Backup"
if ($Prev) { Write-Host "Rollback: stop the app, delete $InstallDir, rename $Prev back." }
