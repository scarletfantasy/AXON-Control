# Install requirements-build.txt into your Python environment before building.
[CmdletBinding()]
param(
    [string]$Python = 'python',
    [string]$Destination = (Join-Path $PSScriptRoot 'dist')
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'AXON Control currently requires Windows.' }
$axonBuild = Join-Path $PSScriptRoot 'build'
$axonArgs = @(
    '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed', '--onedir',
    '--name', 'AXONControl', '--icon', (Join-Path $PSScriptRoot 'axon-icon.ico'),
    '--add-data', ((Join-Path $PSScriptRoot 'axon-icon.ico') + ';.'),
    '--add-data', ((Join-Path $PSScriptRoot 'axon-icon.png') + ';.'),
    '--collect-all', 'pyaudiowpatch', '--hidden-import', 'numpy',
    '--exclude-module', 'matplotlib', '--exclude-module', 'pandas',
    '--exclude-module', 'scipy', '--exclude-module', 'IPython',
    '--distpath', $Destination, '--workpath', (Join-Path $axonBuild 'cache'),
    '--specpath', $axonBuild, (Join-Path $PSScriptRoot 'AXON Control.pyw')
)
& $Python @axonArgs
if ($LASTEXITCODE -ne 0) { throw 'Standalone build failed.' }
Write-Host ('Build ready: ' + (Join-Path $Destination 'AXONControl\AXONControl.exe'))
