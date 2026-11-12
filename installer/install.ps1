# Subtle installer. Run "Install Subtle.bat" (it calls this). Running it again repairs or updates.
#
# Everything goes inside the Subtle folder: a private Python (runtime\), the built-in translator
# (bin\llama\) and the models (models\). Nothing is installed system-wide, so uninstalling is
# deleting the folder (and the shortcuts).
param(
    [string]$Variant = "",          # cuda13 | cuda12 | vulkan | cpu (default: detected)
    [switch]$NoShortcuts,
    [switch]$NoLaunch,
    [switch]$Unattended             # never wait for a key press
)

# "Continue", not "Stop": Windows PowerShell 5.1 turns any stderr output of a native program (uv and
# pip print progress there) into an error. Native commands are checked with $LASTEXITCODE instead,
# and the cmdlets that matter use -ErrorAction Stop; anything unexpected is caught by the trap.
$ErrorActionPreference = "Continue"
trap { Fail "Unexpected error: $_" }
$ProgressPreference = "SilentlyContinue"          # Invoke-WebRequest is very slow with its progress bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = Split-Path -Parent $PSScriptRoot
$Runtime = Join-Path $Root "runtime"
$Venv = Join-Path $Runtime "venv"
$Downloads = Join-Path $Runtime "downloads"
$LogDir = Join-Path $Root "logs"
New-Item -ItemType Directory -Force $Runtime, $Downloads, $LogDir | Out-Null
try { Start-Transcript -Path (Join-Path $LogDir "install.log") -Force | Out-Null } catch {}

$Versions = Get-Content (Join-Path $PSScriptRoot "versions.json") -Raw | ConvertFrom-Json

function Say($text, $color = "Gray") { Write-Host $text -ForegroundColor $color }
function Step($text) { Write-Host ""; Write-Host "==> $text" -ForegroundColor Cyan }
function Fail($text) {
    Write-Host ""
    Write-Host "Setup could not finish:" -ForegroundColor Red
    Write-Host "  $text" -ForegroundColor Red
    Write-Host ""
    Write-Host "The full log is in logs\install.log. Run Install Subtle.bat again to retry;" -ForegroundColor Yellow
    Write-Host "finished parts are kept, so it continues where it stopped." -ForegroundColor Yellow
    try { Stop-Transcript | Out-Null } catch {}
    if (-not $Unattended) { Read-Host "Press Enter to close" | Out-Null }
    exit 1
}

function Get-File($url, $dest, $sha256, $label) {
    if ((Test-Path $dest) -and $sha256) {
        if ((Get-FileHash $dest -Algorithm SHA256).Hash.ToLower() -eq $sha256.ToLower()) { Say "  $label already downloaded"; return }
    }
    Say "  Downloading $label..."
    $curl = Join-Path $env:SystemRoot "System32\curl.exe"
    if (Test-Path $curl) {
        # resumes a half-finished download (-C -), retries on network hiccups
        & $curl -L --fail --retry 5 --retry-delay 3 -C - --progress-bar -o "$dest" "$url"
        if ($LASTEXITCODE -ne 0) {
            Remove-Item $dest -ErrorAction SilentlyContinue
            & $curl -L --fail --retry 5 --retry-delay 3 --progress-bar -o "$dest" "$url"
            if ($LASTEXITCODE -ne 0) { Fail "Download failed: $url (check your internet connection)" }
        }
    } else {
        Invoke-WebRequest -Uri $url -OutFile $dest -UseBasicParsing
    }
    if ($sha256) {
        $got = (Get-FileHash $dest -Algorithm SHA256).Hash.ToLower()
        if ($got -ne $sha256.ToLower()) {
            Remove-Item $dest -ErrorAction SilentlyContinue
            Fail "$label is damaged (checksum mismatch). It was deleted; please run the installer again."
        }
    }
}

Write-Host ""
Write-Host "  Subtle setup" -ForegroundColor White
Write-Host "  Real-time subtitles and translation for any video, on your own PC." -ForegroundColor Gray
Write-Host "  Installs into: $Root" -ForegroundColor Gray

# ------------------------------------------------------------------------------------------------
Step "Checking your PC"
if (-not [Environment]::Is64BitOperatingSystem) { Fail "Subtle needs 64-bit Windows 10 or 11." }
$os = [Environment]::OSVersion.Version
if ($os.Major -lt 10) { Fail "Subtle needs Windows 10 or 11." }

$drive = (Get-Item $Root).PSDrive
$freeGB = [math]::Round($drive.Free / 1GB, 1)
Say "  Free disk space on drive $($drive.Name): $freeGB GB"
if ($freeGB -lt 8) { Fail "Not enough disk space: Subtle needs about 8 GB for itself plus 2-10 GB for models ($freeGB GB free)." }

$gpuName = ""; $gpuMem = 0; $cc = 0.0; $cudaVer = 0.0; $driver = ""
$smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
if (-not $smi -and (Test-Path "$env:SystemRoot\System32\nvidia-smi.exe")) { $smi = "$env:SystemRoot\System32\nvidia-smi.exe" }
if ($smi) {
    try {
        $q = & $smi --query-gpu=name,memory.total,compute_cap,driver_version --format=csv,noheader,nounits 2>$null | Select-Object -First 1
        $parts = $q -split ",\s*"
        $gpuName = $parts[0]; $gpuMem = [int]$parts[1]; $cc = [double]$parts[2]; $driver = $parts[3]
        $head = (& $smi 2>$null) -join "`n"
        if ($head -match "CUDA (?:UMD )?Version:\s*([\d.]+)") { $cudaVer = [double]$Matches[1] }
    } catch { $gpuName = "" }
}
$otherGpu = ""
if (-not $gpuName) {
    try { $otherGpu = (Get-CimInstance Win32_VideoController | Where-Object { $_.Name -notmatch "Basic|Remote|Virtual" } | Select-Object -First 1).Name } catch {}
}

if (-not $Variant) {
    if ($gpuName) {
        Say ("  Graphics card: $gpuName, {0:N0} GB video memory, driver $driver (CUDA $cudaVer)" -f ($gpuMem / 1024)) "Green"
        if ($cc -ge 7.5 -and $cudaVer -ge 13.0) { $Variant = "cuda13" }
        elseif ($cc -lt 7.5 -and $cudaVer -ge 12.6) { $Variant = "cuda12" }
        elseif ($cc -ge 7.5 -and $cc -lt 10.0 -and $cudaVer -ge 12.6) {
            Say "  Your NVIDIA driver is a bit old. It works, but updating it (nvidia.com/drivers) is recommended." "Yellow"
            $Variant = "cuda12"
        } else {
            Say "  Your NVIDIA driver ($driver) is too old for this graphics card." "Yellow"
            Say "  Please update it from https://www.nvidia.com/drivers (or the NVIDIA app), then run this setup again." "Yellow"
            if ($Unattended) { Fail "NVIDIA driver too old" }
            $ans = Read-Host "  Continue anyway without GPU speed-up (slow)? [y/N]"
            if ($ans -notmatch "^[yY]") { Fail "Please update the NVIDIA driver first." }
            $Variant = "cpu"
        }
    } elseif ($otherGpu) {
        Say "  Graphics card: $otherGpu (not NVIDIA)." "Yellow"
        Say "  Speech recognition will run on the processor (slower; use the small/medium speech models)." "Yellow"
        Say "  The translator will still use your graphics card." "Yellow"
        $Variant = "vulkan"
    } else {
        Say "  No graphics card found - everything runs on the processor (slow)." "Yellow"
        $Variant = "cpu"
    }
}
Say "  Setup type: $Variant ($($Versions.llama_cpp.variants.$Variant.note))"
Set-Content -Path (Join-Path $Runtime "variant.txt") -Value $Variant

# ------------------------------------------------------------------------------------------------
Step "Getting the Python runtime (private to Subtle)"
$uvDir = Join-Path $Runtime "uv"
$uv = Join-Path $uvDir "uv.exe"
$uvZip = Join-Path $Downloads "uv.zip"
if (-not (Test-Path $uv) -or -not ((& $uv --version 2>$null) -match [regex]::Escape($Versions.uv.version))) {
    Get-File $Versions.uv.url $uvZip $Versions.uv.sha256 "uv $($Versions.uv.version)"
    New-Item -ItemType Directory -Force $uvDir | Out-Null
    Expand-Archive -Path $uvZip -DestinationPath $uvDir -Force -ErrorAction Stop
}
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Runtime "python"
$env:UV_CACHE_DIR = Join-Path $Runtime "uv-cache"
$env:UV_LINK_MODE = "copy"
$env:UV_PYTHON_PREFERENCE = "only-managed"
$env:UV_NO_PROGRESS = ""

& $uv python install $Versions.python
if ($LASTEXITCODE -ne 0) { Fail "Could not get Python $($Versions.python)." }
$py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    & $uv venv $Venv --python $Versions.python
    if ($LASTEXITCODE -ne 0) { Fail "Could not create the Python environment." }
}

Step "Installing Subtle's packages (about 3 GB the first time - this takes a while)"
$index = $Versions.torch_index.$Variant
$req = Join-Path $PSScriptRoot "requirements.txt"
$torchPins = Get-Content $req | Where-Object { $_ -match "^(torch|torchaudio)==" }
& $uv pip install --python $py --index-url $index @torchPins
if ($LASTEXITCODE -ne 0) { Fail "Could not install PyTorch from $index." }
& $uv pip install --python $py -r $req
if ($LASTEXITCODE -ne 0) { Fail "Could not install the Python packages." }
# Subtle Lite (edition.txt says "lite") is the extension's server only: no packages for the extra features.
$edition = Join-Path $Root "edition.txt"
$lite = (Test-Path $edition) -and ((Get-Content $edition -Raw).Trim() -eq "lite")
if (-not $lite) {
    & $uv pip install --python $py -r (Join-Path $PSScriptRoot "requirements-extras.txt")
    if ($LASTEXITCODE -ne 0) { Say "  Warning: the packages for the extra features did not install (subtitles for any app, learning mode). Everything else works." "Yellow" }
}
& $uv cache clean *> $null          # the downloaded wheels are no longer needed (~3 GB)

Step "Checking the installation"
$check = (& $py -c "import torch, tokenizers, safetensors, websockets, silero_vad, onnxruntime, pystray, tkinter; print(torch.__version__, torch.cuda.is_available())" 2>$null) -join " "
Say "  $check"
if ($LASTEXITCODE -ne 0) { Fail "The Python packages don't load: $check" }
if ($Variant -like "cuda*" -and "$check" -notmatch "True$") {
    Say "  Warning: PyTorch can't use your NVIDIA card. Update the NVIDIA driver, then run this setup again." "Yellow"
}

# ------------------------------------------------------------------------------------------------
Step "Getting the built-in translator (llama.cpp $($Versions.llama_cpp.build))"
$llamaDir = Join-Path $Root "bin\llama"
$stamp = Join-Path $llamaDir "subtle-build.txt"
$want = "$($Versions.llama_cpp.build) $Variant"
if ((Test-Path $stamp) -and ((Get-Content $stamp -Raw).Trim() -eq $want) -and (Test-Path (Join-Path $llamaDir "llama-server.exe"))) {
    Say "  Already installed"
} else {
    if (Test-Path $llamaDir) { Remove-Item $llamaDir -Recurse -Force }
    New-Item -ItemType Directory -Force $llamaDir | Out-Null
    foreach ($f in $Versions.llama_cpp.variants.$Variant.files) {
        $zip = Join-Path $Downloads $f.name
        Get-File $f.url $zip $f.sha256 $f.name
        $tmp = Join-Path $Downloads "unzip"
        if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
        Expand-Archive -Path $zip -DestinationPath $tmp -Force -ErrorAction Stop
        Get-ChildItem $tmp -Recurse -File | Move-Item -Destination $llamaDir -Force -ErrorAction Stop
        Remove-Item $tmp -Recurse -Force
    }
    Set-Content -Path $stamp -Value $want
}
# Only llama-server and its libraries are needed; the other llama.cpp tools are left out to save space.
Get-ChildItem $llamaDir -Filter "*.exe" | Where-Object { $_.Name -ne "llama-server.exe" } | Remove-Item -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $Downloads "llama-*.zip"), (Join-Path $Downloads "cudart-*.zip") -ErrorAction SilentlyContinue

foreach ($d in "models\whisper", "models\llm", "models\speaker") { New-Item -ItemType Directory -Force (Join-Path $Root $d) | Out-Null }

# ------------------------------------------------------------------------------------------------
$pyw = Join-Path $Venv "Scripts\pythonw.exe"
$app = Join-Path $Root "server\supervisor.py"
$icon = Join-Path $Root "server\web\subtle.ico"
if (-not $NoShortcuts) {
    Step "Creating shortcuts"
    $shell = New-Object -ComObject WScript.Shell
    $places = @([Environment]::GetFolderPath("Desktop"), (Join-Path ([Environment]::GetFolderPath("Programs")) ""))
    foreach ($dir in $places) {
        $lnk = $shell.CreateShortcut((Join-Path $dir "Subtle.lnk"))
        $lnk.TargetPath = $pyw
        $lnk.Arguments = "`"$app`""
        $lnk.WorkingDirectory = $Root
        $lnk.IconLocation = $icon
        $lnk.Description = "Subtle - real-time subtitles"
        $lnk.Save()
    }
    Say "  Desktop and Start-menu shortcut: Subtle"
}

Step "Done"
Say "  Subtle is installed. Next:" "Green"
Say "   1. Subtle opens its control panel in your browser - follow the Setup guide there"
Say "      to download the models that fit your PC and add the browser extension."
Say "   2. Later, start Subtle from the desktop or Start-menu shortcut. It waits in the tray"
Say "      (bottom right) and uses no video memory until you turn subtitles on."
try { Stop-Transcript | Out-Null } catch {}

if (-not $NoLaunch) {
    Start-Process -FilePath $pyw -ArgumentList "`"$app`"" -WorkingDirectory $Root
}
if (-not $Unattended) { Start-Sleep -Seconds 3 }
