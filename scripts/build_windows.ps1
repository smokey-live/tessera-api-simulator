$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$WindowsDir = Join-Path $Root "packaging\windows"
$AssetsDir = Join-Path $WindowsDir "assets"
$IconPng = Join-Path $AssetsDir "sl-icon.png"
$IconIco = Join-Path $AssetsDir "sl-icon.ico"
$VenvDir = Join-Path $Root ".venv-windows"
$Python = Join-Path $VenvDir "Scripts\python.exe"

function Get-SystemPython {
    $Candidates = @(
        @("py", "-3"),
        @("python"),
        @("python3")
    )

    foreach ($Candidate in $Candidates) {
        $Command = $Candidate[0]
        if (Get-Command $Command -ErrorAction SilentlyContinue) {
            return $Candidate
        }
    }

    throw "Python 3 was not found. Install Python from https://www.python.org/downloads/windows/ and enable 'Add python.exe to PATH', then open a new PowerShell window and run this script again."
}

if (!(Test-Path $IconPng)) {
    throw "Missing icon source: $IconPng"
}

if (!(Test-Path $Python)) {
    $SystemPython = Get-SystemPython
    $Command = $SystemPython[0]
    $Arguments = @()
    if ($SystemPython.Length -gt 1) {
        $Arguments += $SystemPython[1..($SystemPython.Length - 1)]
    }
    $Arguments += @("-m", "venv", $VenvDir)
    & $Command @Arguments
}

& $Python -m pip install --upgrade pip
& $Python -m pip install -r (Join-Path $WindowsDir "requirements-windows.txt")

& $Python -c "from PIL import Image; img=Image.open(r'$IconPng'); img.save(r'$IconIco', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])"
& $Python -m PyInstaller --clean --noconfirm (Join-Path $WindowsDir "TesseraMonitoringAndControl.spec")

Write-Host ""
Write-Host "Built Windows executable:"
Write-Host (Join-Path $Root "dist\TesseraMonitoringAndControl.exe")
Write-Host ""
Write-Host "Run as Administrator if Windows blocks binding to TCP 23 or UDP 514."
