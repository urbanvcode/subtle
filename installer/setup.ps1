# Subtle-Setup.exe runs this (from a temporary folder, next to payload.zip): pick a folder, unpack Subtle
# there, then run Install Subtle.bat. Running it over an existing Subtle updates it; models, settings and
# the private Python are kept.
param(
    [string]$Target = "",
    [switch]$NoInstall          # only unpack (for testing)
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$zip = Join-Path $here "payload.zip"

Add-Type -AssemblyName System.Windows.Forms
if (-not $Target) {
    $default = Join-Path $env:USERPROFILE "Subtle"
    $msg = "Subtle will be installed in:`n`n$default`n`nIt needs about 8 GB, plus 2-10 GB for models.`n`n" +
           "Yes: install there.`nNo: choose another folder.`nCancel: stop."
    $answer = [System.Windows.Forms.MessageBox]::Show($msg, "Subtle setup", "YesNoCancel", "Information")
    if ($answer -eq "Cancel") { exit 1 }
    if ($answer -eq "Yes") {
        $Target = $default
    } else {
        $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
        $dlg.Description = "Choose a folder for Subtle (a 'Subtle' folder is made inside it)"
        $dlg.ShowNewFolderButton = $true
        if ($dlg.ShowDialog() -ne "OK") { exit 1 }
        $Target = Join-Path $dlg.SelectedPath "Subtle"
    }
}

Write-Host "Unpacking Subtle into $Target ..."
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("subtle-setup-" + [guid]::NewGuid().ToString("N"))
Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
$src = Join-Path $tmp "Subtle"
New-Item -ItemType Directory -Force $Target | Out-Null
# copy the program files over; models\, runtime\, bin\, settings.json and logs\ are never in the zip, so they stay
Copy-Item -Path (Join-Path $src "*") -Destination $Target -Recurse -Force
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue

if ($NoInstall) { Write-Host "Unpacked (not installing)."; exit 0 }
$bat = Join-Path $Target "Install Subtle.bat"
Start-Process -FilePath $bat -WorkingDirectory $Target
