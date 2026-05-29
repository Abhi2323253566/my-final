# Zoom Worker v8.3.2 - One-Click Installer for Windows RDP / VPS
# Installs: Python + Playwright Chromium + v8.3.2 pool worker (prewarm + tap-and-join + admin-cap)
# Usage (run in PowerShell as Administrator):
#   $env:DASHBOARD_URL="https://your-dashboard"; iwr "$env:DASHBOARD_URL/api/worker/install.ps1" | iex

$ErrorActionPreference = "Stop"
$ProgressPreference    = "SilentlyContinue"

$WorkerDir  = "C:\zoom-worker"
$BackendUrl = $env:DASHBOARD_URL
if (-not $BackendUrl) {
    $BackendUrl = "https://rdp-pool-manager.preview.emergentagent.com"
}

function Section($t) {
    Write-Host ""
    Write-Host "===> $t" -ForegroundColor Cyan
}

# 0. Admin check
$isAdmin = ([Security.Principal.WindowsPrincipal] `
    [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(`
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Please run this in an *Administrator* PowerShell window." -ForegroundColor Red
    exit 1
}

# 0b. Stop any existing ZoomWorker service so we can overwrite files cleanly
Section "Stopping any existing ZoomWorker service / process"
try { Stop-Service -Name "ZoomWorker" -Force -ErrorAction SilentlyContinue } catch {}
try { Get-Process python* -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "*zoom-worker*" } | Stop-Process -Force -ErrorAction SilentlyContinue } catch {}
Start-Sleep -Seconds 2

Section "Creating $WorkerDir"
New-Item -ItemType Directory -Force -Path $WorkerDir | Out-Null
Set-Location $WorkerDir

# 1. Install Python if missing (3.11+ required for Playwright async)
Section "Checking Python (need >= 3.11)"
$pythonCmd = $null
foreach ($c in @("python", "py")) {
    if (Get-Command $c -ErrorAction SilentlyContinue) {
        $ver = & $c --version 2>&1
        if ($ver -match "Python 3\.(\d+)") {
            $minor = [int]$matches[1]
            if ($minor -ge 11) { $pythonCmd = $c; break }
        }
    }
}
if (-not $pythonCmd) {
    Write-Host "Installing Python 3.11.9..."
    $pyInstaller = "$env:TEMP\python-3.11.9-installer.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe" -OutFile $pyInstaller
    Start-Process -Wait -FilePath $pyInstaller -ArgumentList "/quiet","InstallAllUsers=1","PrependPath=1","Include_test=0"
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
    $pythonCmd = "python"
}
Write-Host "Python OK: $(& $pythonCmd --version)" -ForegroundColor Green

# 2. Back up any old worker file before overwriting
Section "Backing up old worker (if any)"
$ts = Get-Date -Format "yyyyMMdd-HHmmss"
foreach ($f in @("zoom_worker.py", "zoom_worker_pool.py", ".env")) {
    if (Test-Path "$WorkerDir\$f") {
        Copy-Item "$WorkerDir\$f" "$WorkerDir\$f.backup.$ts" -Force
        Write-Host "  backed up $f -> $f.backup.$ts" -ForegroundColor DarkGray
    }
}

# 3. Download v8.3.2 pool worker (NOT the legacy zoom_worker.py)
Section "Downloading v8.3.2 worker files from $BackendUrl"
Invoke-WebRequest "$BackendUrl/api/worker/zoom_worker_pool.py" -OutFile "$WorkerDir\zoom_worker_pool.py"
$lineCount = (Get-Content "$WorkerDir\zoom_worker_pool.py" | Measure-Object -Line).Lines
Write-Host "  zoom_worker_pool.py downloaded ($lineCount lines)" -ForegroundColor Green
Invoke-WebRequest "$BackendUrl/api/worker/requirements.txt" -OutFile "$WorkerDir\requirements.txt"
Write-Host "  requirements.txt downloaded" -ForegroundColor Green

# 4. Install python deps + Playwright Chromium
Section "Installing Python libraries + Playwright Chromium (~150MB download)"
& $pythonCmd -m pip install --upgrade pip --quiet
& $pythonCmd -m pip install -r "$WorkerDir\requirements.txt" --quiet
& $pythonCmd -m pip install playwright psutil python-dotenv --quiet
Write-Host "  Python libs installed" -ForegroundColor Green
Write-Host "  Installing Playwright Chromium (this takes a minute)..." -ForegroundColor Yellow
& $pythonCmd -m playwright install chromium
Write-Host "  Playwright Chromium installed" -ForegroundColor Green

# 5. Prompt for token (preserve from existing .env if present)
Section "Configure worker"
$existingToken = ""
if (Test-Path "$WorkerDir\.env.backup.$ts") {
    $existingToken = (Select-String -Path "$WorkerDir\.env.backup.$ts" -Pattern "^WORKER_TOKEN=" -SimpleMatch | Select-Object -First 1).Line
    if ($existingToken) {
        $existingToken = $existingToken -replace "^WORKER_TOKEN=", ""
        $existingToken = $existingToken.Trim('"').Trim()
    }
}
if ($existingToken -and $existingToken -match "^[a-f0-9-]+\.[A-Za-z0-9_-]+$") {
    Write-Host "  Reusing WORKER_TOKEN from previous .env backup" -ForegroundColor Green
    $token = $existingToken
} else {
    Write-Host "Open the dashboard, click 'Workers' -> 'Add Worker' -> copy the token shown ONCE." -ForegroundColor Yellow
    Write-Host "Token format: <uuid>.<secret>" -ForegroundColor Yellow
    $token = Read-Host "Paste WORKER_TOKEN"
    if (-not $token -or $token -notmatch "^[a-f0-9-]+\.[A-Za-z0-9_-]+$") {
        Write-Host "Invalid token format. Re-run the installer." -ForegroundColor Red
        exit 1
    }
}

# 6. Write .env with v8.3.2 prewarm + tap-and-join defaults
Section "Writing .env (v8.3.2 defaults)"
$envText = @"
# ============================================================
#  Zoom Worker v8.3.2 - generated $(Get-Date)
#  Edit any value below, then: Restart-Service ZoomWorker
# ============================================================

DASHBOARD_URL=$BackendUrl
WORKER_TOKEN=$token

# ---- Polling / capacity ----
POLL_INTERVAL=5
SPAWN_DELAY_MS=120
SPAWN_BATCH=8
MAX_CONCURRENT_TASKS=5

# ---- v8.3.4: Headless + offscreen + mute-on-join ----
# HEADLESS=true: chromium runs invisibly. Set to false ONLY for debugging.
# OFFSCREEN_WINDOW=true: when HEADLESS=false, pushes window off-screen so RDP
#   user never sees floating browsers.
# JOIN_WITH_AUDIO_MUTED=true: bot enters meeting with mic muted (pre-toggled on preview)
# JOIN_WITH_VIDEO_OFF=true: bot enters meeting with camera off (pre-toggled on preview)
HEADLESS=true
OFFSCREEN_WINDOW=true
JOIN_WITH_AUDIO_MUTED=true
JOIN_WITH_VIDEO_OFF=true

AUTO_CAPACITY=true

# ---- Browser pool sizing ----
TABS_PER_BROWSER=20

# ---- v8.1 PREWARM engine ----
# IMPORTANT: PREWARM_CONTEXTS auto-scales to admin capacity_max at runtime.
# These are just the boot-time defaults; the worker will grow the ready pool
# to match whatever cap the dashboard says (jitna limit, utne ready).
PREWARM_ENABLED=true
PREWARM_BROWSERS=2
PREWARM_CONTEXTS=10
PREWARM_MIN_READY=5
PREWARM_MAX_READY=20
PREWARM_PRELOAD_URL=https://app.zoom.us/wc/join
WARMUP_INTERVAL_SEC=15
SHRINK_IDLE_SEC=120

# v8.3.3: scale ready pool to match admin capacity_max (browsers grow with it)
PREWARM_MATCH_ADMIN_CAP=true

# ---- v8.3 TAP-AND-JOIN ----
PERSISTENT_CACHE=true
PERSISTENT_CACHE_DIR=C:\zoom-worker\disk-cache
PERSISTENT_CACHE_SIZE_MB=256
STORAGE_STATE_PATH=C:\zoom-worker\storage-state.json
STORAGE_STATE_REFRESH_HOURS=24
FORM_PREWARM_WAIT_MS=1500

# ---- v8.3.1 DEBUG ----
DEBUG_DUMP_DOM=true
DEBUG_DUMP_DIR=C:\zoom-worker\debug

# ---- Cleanup ----
CLEANUP_INTERVAL_SEC=300

# Optional: local names file (overrides dashboard Name source if set)
LOCAL_NAMES_FILE=
"@
[IO.File]::WriteAllText("$WorkerDir\.env", $envText, [Text.UTF8Encoding]::new($false))
Write-Host "  .env saved with v8.3.2 prewarm defaults" -ForegroundColor Green

# Wipe stale storage_state so bootstrap runs fresh
Remove-Item "$WorkerDir\storage-state.json" -ErrorAction SilentlyContinue
Remove-Item "C:\zoom-worker\storage-state.json" -ErrorAction SilentlyContinue

# 7. Install as Windows service via NSSM
Section "Install Windows service (auto-restart on boot)"
$installService = Read-Host "Install as a Windows service so it auto-starts on reboot? (Y/n)"
if ($installService -ne "n" -and $installService -ne "N") {
    $nssmZip = "$env:TEMP\nssm.zip"
    $nssmDir = "C:\tools\nssm"
    if (-not (Test-Path "$nssmDir\nssm.exe")) {
        Invoke-WebRequest "https://nssm.cc/release/nssm-2.24.zip" -OutFile $nssmZip
        Expand-Archive $nssmZip -DestinationPath "$env:TEMP\nssm-ex" -Force
        New-Item -ItemType Directory -Force -Path $nssmDir | Out-Null
        Copy-Item "$env:TEMP\nssm-ex\nssm-2.24\win64\nssm.exe" "$nssmDir\nssm.exe" -Force
    }
    $svc = "ZoomWorker"
    & "$nssmDir\nssm.exe" stop   $svc 2>$null | Out-Null
    & "$nssmDir\nssm.exe" remove $svc confirm 2>$null | Out-Null
    & "$nssmDir\nssm.exe" install $svc (Get-Command $pythonCmd).Source "$WorkerDir\zoom_worker_pool.py"
    & "$nssmDir\nssm.exe" set $svc AppDirectory $WorkerDir
    & "$nssmDir\nssm.exe" set $svc AppStdout    "$WorkerDir\worker.log"
    & "$nssmDir\nssm.exe" set $svc AppStderr    "$WorkerDir\worker.err.log"
    & "$nssmDir\nssm.exe" set $svc Start        SERVICE_AUTO_START
    & "$nssmDir\nssm.exe" set $svc AppRestartDelay 5000
    & "$nssmDir\nssm.exe" start $svc
    Write-Host "  Service 'ZoomWorker' installed and started" -ForegroundColor Green
    Write-Host "  Logs:   $WorkerDir\worker.log" -ForegroundColor Yellow
    Write-Host "  Manage: net stop ZoomWorker  /  net start ZoomWorker" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "===> Wait ~30 seconds, then check dashboard /workers page" -ForegroundColor Cyan
    Write-Host "     Expected: Pool column shows 'v8.3.2-admin-cap' + 'state 0.0h'" -ForegroundColor Cyan
    Start-Sleep -Seconds 8
    if (Test-Path "$WorkerDir\worker.log") {
        Write-Host ""
        Write-Host "===> First lines of worker.log:" -ForegroundColor Cyan
        Get-Content "$WorkerDir\worker.log" -Tail 25 | ForEach-Object { Write-Host "  $_" -ForegroundColor DarkGray }
    }
} else {
    Section "Running worker in foreground (Ctrl+C to stop)"
    Set-Location $WorkerDir
    & $pythonCmd zoom_worker_pool.py
}
