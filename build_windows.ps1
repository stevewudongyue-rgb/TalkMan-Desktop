# Build Talk Man as a Windows desktop application.
# Output: dist-desktop\\TalkMan\\TalkMan.exe
[CmdletBinding()]
param(
    [switch]$Clean,
    [ValidatePattern('^dist-[a-z0-9-]+$')]
    [string]$OutputDirectory = 'dist-desktop'
)

$ErrorActionPreference = 'Stop'
$script = Join-Path $PSScriptRoot 'talkman_app.py'
$dist = Join-Path $PSScriptRoot $OutputDirectory
$work = Join-Path $PSScriptRoot 'build'
$spec = Join-Path $work 'TalkMan.spec'
$candidates = @(
    "$env:USERPROFILE\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe",
    "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe",
    'py', 'python'
)

$python = $null
foreach ($candidate in $candidates) {
    if ($candidate -eq 'py' -or $candidate -eq 'python') {
        if (Get-Command $candidate -ErrorAction SilentlyContinue) {
            $python = $candidate
            break
        }
    } elseif (Test-Path -LiteralPath $candidate) {
        $python = $candidate
        break
    }
}
if (-not $python) {
    throw 'Python 3.10+ was not found. Install Python with Tcl/Tk support.'
}

& $python -c "import tkinter, sys; assert sys.version_info >= (3,10)"
if ($LASTEXITCODE -ne 0) {
    throw 'Python is missing tkinter or is older than 3.10.'
}
$libs = Join-Path $PSScriptRoot 'TalkMan-libs'
$env:PYTHONPATH = $libs + [IO.Path]::PathSeparator + $env:PYTHONPATH
& $python -m PyInstaller --version
if ($LASTEXITCODE -ne 0) {
    throw 'PyInstaller is missing. Run: python -m pip install pyinstaller'
}

# This runtime imports tkinter but PyInstaller's Tcl probe fails and excludes it.
# Keep normal module analysis and collect Tcl/Tk at the standard runtime paths.
$pythonRoot = (& $python -c "import sys; print(sys.prefix)").Trim()
$hooks = Join-Path $PSScriptRoot 'TalkMan-build-hooks'
if (-not (Test-Path -LiteralPath (Join-Path $libs 'sounddevice.py'))) {
    throw 'Install project dependencies from TalkMan-requirements.txt into TalkMan-libs first.'
}
& $python -c "import sounddevice, websocket, cffi, certifi"
if ($LASTEXITCODE -ne 0) { throw 'Audio dependencies could not be imported.' }
$tkArgs = @()
$tkFiles = @(
    @{ Path = Join-Path $pythonRoot 'DLLs\_tkinter.pyd'; Kind = 'binary'; Destination = '.' },
    @{ Path = Join-Path $pythonRoot 'DLLs\tcl86t.dll'; Kind = 'binary'; Destination = '.' },
    @{ Path = Join-Path $pythonRoot 'DLLs\tk86t.dll'; Kind = 'binary'; Destination = '.' },
    @{ Path = Join-Path $pythonRoot 'tcl\tcl8.6'; Kind = 'data'; Destination = '_tcl_data' },
    @{ Path = Join-Path $pythonRoot 'tcl\tk8.6'; Kind = 'data'; Destination = '_tk_data' }
)
foreach ($tkFile in $tkFiles) {
    if (Test-Path -LiteralPath $tkFile.Path) {
        $tkArgs += if ($tkFile.Kind -eq 'binary') {
            @('--add-binary', "$($tkFile.Path);$($tkFile.Destination)")
        } else {
            @('--add-data', "$($tkFile.Path);$($tkFile.Destination)")
        }
    }
}

if ($Clean) {
    $projectRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\') + '\'
    foreach ($outputPath in @($dist, $work)) {
        $absoluteOutput = [IO.Path]::GetFullPath($outputPath)
        if (-not $absoluteOutput.StartsWith($projectRoot, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to clean a directory outside the project: $absoluteOutput"
        }
    }
    Remove-Item -LiteralPath $dist, $work -Recurse -Force -ErrorAction SilentlyContinue
}
New-Item -ItemType Directory -Force -Path $dist, $work | Out-Null
$versionFile = Join-Path $work 'TalkMan-version.txt'
& $python (Join-Path $PSScriptRoot 'talkman_version.py') --windows-resource $versionFile
if ($LASTEXITCODE -ne 0) { throw 'Version metadata generation failed.' }
& $python (Join-Path $PSScriptRoot ([regex]::Unescape("TalkMan-\u6536\u96c6\u7b2c\u4e09\u65b9\u8bb8\u53ef.py")))
if ($LASTEXITCODE -ne 0) { throw "Third-party notices collection failed." }
$pyinstallerArgs = @(
    '--noconfirm', '--clean', '--windowed', '--onedir',
    '--name', 'TalkMan', '--icon', (Join-Path $PSScriptRoot 'TalkMan-assets\app\TalkMan.ico'),
    '--version-file', $versionFile, '--distpath', $dist, '--workpath', $work,
    '--specpath', $work, '--additional-hooks-dir', $hooks,
    '--paths', $libs, '--collect-all', '_sounddevice_data', '--collect-submodules', 'comtypes',
    '--hidden-import', '_cffi_backend', '--hidden-import', 'comtypes',
    '--hidden-import', 'comtypes.client', '--hidden-import', 'comtypes.gen.UIAutomationClient',
    # Keep the source ASCII so Windows PowerShell 5.1 reads UTF-8 scripts correctly.
    '--collect-data', 'certifi', '--runtime-hook', (Join-Path $PSScriptRoot ([regex]::Unescape('TalkMan-\u8bc1\u4e66\u94a9\u5b50.py'))),
    '--add-data', ((Join-Path $PSScriptRoot 'TalkMan-assets') + ';TalkMan-assets')
) + $tkArgs + @($script)
& $python -m PyInstaller @pyinstallerArgs
if ($LASTEXITCODE -ne 0) {
    throw 'PyInstaller build failed.'
}

$exe = Join-Path $dist 'TalkMan\TalkMan.exe'
if (-not (Test-Path -LiteralPath $exe)) {
    throw "The expected executable was not found: $exe"
}
$requiredTkFiles = @(
    (Join-Path $dist 'TalkMan\_internal\_tkinter.pyd'),
    (Join-Path $dist 'TalkMan\_internal\tcl86t.dll'),
    (Join-Path $dist 'TalkMan\_internal\tk86t.dll'),
    (Join-Path $dist 'TalkMan\_internal\_tcl_data\init.tcl'),
    (Join-Path $dist 'TalkMan\_internal\_tk_data\tk.tcl')
)
foreach ($requiredTkFile in $requiredTkFiles) {
    if (-not (Test-Path -LiteralPath $requiredTkFile)) {
        throw "The packaged Tcl/Tk runtime is incomplete: $requiredTkFile"
    }
}
$pyz = Join-Path $work 'TalkMan\PYZ-00.pyz'
& $python -c "import sys; from pathlib import Path; from PyInstaller.archive.readers import ZlibArchiveReader; modules = ZlibArchiveReader(sys.argv[1]).toc; required = {'tkinter', 'tkinter.ttk', 'talkman_qwen', 'talkman_version', 'talkman_updates', 'talkman_update_ui', 'talkman_license', 'talkman_license_ui', 'cryptography', 'talkman_free_service', 'talkman_ai_edit', 'talkman_expression', 'talkman_conventions', 'talkman_conventions_ui', 'talkman_convention_learning', 'talkman_delivery_ui', 'talkman_translation', 'talkman_translation_ui', 'talkman_mobile_bridge', 'talkman_mobile_protocol', 'talkman_voice', 'talkman_asr', 'talkman_assets', 'talkman_asr_volc_baidu', 'talkman_asr_tencent_xunfei', 'talkman_shortcuts', 'talkman_magnifier', 'talkman_target', 'talkman_target_windows', 'talkman_settings_ui', 'talkman_settings_viewport', 'talkman_ui_widgets', 'talkman_editor', 'talkman_credentials', 'talkman_onboarding', 'talkman_hover_help', 'talkman_target_picker', 'talkman_input_positions', 'talkman_position_picker', 'talkman_position_windows', 'talkman_windows_coordinates', 'talkman_commands', 'talkman_command_ui', 'talkman_command_intent', 'talkman_sync', 'talkman_live_sync', 'sounddevice', 'websocket', 'ssl', 'wave', 'comtypes.gen.UIAutomationClient'}; missing = required - modules.keys(); assert not missing, f'Packaged modules are missing: {missing}'; assert any(name.startswith('comtypes.gen._944DE083_') for name in modules), 'Missing UI Automation interfaces'; root=Path(sys.argv[2]); assert list(root.rglob('*portaudio*.dll')), 'Missing PortAudio'; assert list(root.rglob('_cffi_backend*.pyd')), 'Missing icons'; assert list(root.rglob('cacert.pem')), 'Missing TLS certificates'" $pyz (Join-Path $dist 'TalkMan')
if ($LASTEXITCODE -ne 0) {
    throw 'The packaged tkinter modules are incomplete.'
}
# Keep the portable package compatible with the updater's required root files.
& $python -c "import shutil, sys; from pathlib import Path; from talkman_updates import WINDOWS_FILES; root=Path(sys.argv[1]); source=Path(sys.argv[2])/'TalkMan-assets'/'legal'; [shutil.copy2(source/name, root/name) for name in WINDOWS_FILES if name not in ('TalkMan.exe', '_internal')]; assert all((root/name).exists() for name in WINDOWS_FILES), 'Incomplete portable package'" (Join-Path $dist 'TalkMan') $PSScriptRoot
if ($LASTEXITCODE -ne 0) {
    throw 'The portable package documentation is incomplete.'
}
Remove-Item -LiteralPath $spec -Force -ErrorAction SilentlyContinue
Write-Host "Build succeeded: $exe" -ForegroundColor Green
