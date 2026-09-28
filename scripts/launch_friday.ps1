# Launcher for the "Launch FRIDAY" desktop shortcut.
#
# Ensures Ollama is running with the configured model, then starts FRIDAY in
# desktop/tray mode by calling the existing START.bat - this script never
# duplicates FRIDAY's own startup logic, it only gates it. Safe to run
# repeatedly: it no-ops on anything already running/installed.

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$DataDir = Join-Path $Root 'data'
$Log = Join-Path $DataDir 'launcher.log'
$Model = 'qwen2.5:3b'
$OllamaUrl = 'http://127.0.0.1:11434'

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

function Write-Log {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Add-Content -Path $Log -Value $line
}

if (-not ('FridayLauncher.NativeMessageBox' -as [type])) {
    Add-Type -Namespace FridayLauncher -Name NativeMessageBox -MemberDefinition @'
[DllImport("user32.dll", CharSet = CharSet.Unicode)]
public static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);
'@
}

function Show-Failure {
    param([string]$Message)
    Write-Log "FAILURE: $Message"
    # A plain user32 MessageBoxW modal works regardless of apartment state;
    # System.Windows.Forms.MessageBox needs STA and silently no-ops here since
    # this script runs under powershell.exe's default MTA.
    $MB_OK = 0x0; $MB_ICONERROR = 0x10; $MB_TOPMOST = 0x40000
    [FridayLauncher.NativeMessageBox]::MessageBoxW(
        [IntPtr]::Zero, $Message, 'FRIDAY launcher', ($MB_OK -bor $MB_ICONERROR -bor $MB_TOPMOST)
    ) | Out-Null
}

function Test-OllamaUp {
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            Invoke-WebRequest -Uri "$OllamaUrl/api/tags" -TimeoutSec 2 -UseBasicParsing | Out-Null
            return $true
        } catch {
            if ($attempt -lt 3) { Start-Sleep -Milliseconds 500 }
        }
    }
    return $false
}

function Get-OllamaCommand {
    $cmd = Get-Command ollama.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { $cmd = Get-Command ollama -ErrorAction SilentlyContinue }
    if ($cmd) { return $cmd.Source }

    # Not on PATH - the Ollama installer doesn't always add one. Check its
    # standard per-user install location before giving up.
    $wellKnown = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
    if (Test-Path $wellKnown) { return $wellKnown }

    return $null
}

try {
    Write-Log '--- launch requested ---'

    # 1. Ollama: reuse if already running, otherwise start it hidden.
    if (Test-OllamaUp) {
        Write-Log 'Ollama already running - reusing it.'
    } else {
        $ollamaExe = Get-OllamaCommand
        if (-not $ollamaExe) {
            Show-Failure "Ollama isn't installed (or not found). Install it from https://ollama.com/download, then try again."
            exit 1
        }

        Write-Log "Starting Ollama ($ollamaExe)..."
        Start-Process -FilePath $ollamaExe -ArgumentList @('serve') -WindowStyle Hidden

        $deadline = (Get-Date).AddSeconds(30)
        while (-not (Test-OllamaUp) -and (Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 1
        }
        if (-not (Test-OllamaUp)) {
            Show-Failure "Ollama didn't come up within 30 seconds. Try running 'ollama serve' manually to see the error."
            exit 1
        }
        Write-Log 'Ollama is up.'
    }

    # 2. Model: pull only if missing.
    $haveModel = $false
    try {
        $tags = Invoke-RestMethod -Uri "$OllamaUrl/api/tags" -TimeoutSec 5
        $haveModel = [bool]($tags.models | Where-Object { $_.name -eq $Model })
    } catch {
        $haveModel = $false
    }

    if ($haveModel) {
        Write-Log "Model $Model already pulled - reusing it."
    } else {
        Write-Log "Model $Model not found - pulling (first run only, this may take a while)..."
        $ollamaExe = Get-OllamaCommand
        if (-not $ollamaExe) {
            Show-Failure "Ollama isn't installed (or not found). Install it from https://ollama.com/download, then try again."
            exit 1
        }
        $pullOut = Join-Path $DataDir 'ollama_pull.log'
        $pullErr = Join-Path $DataDir 'ollama_pull.err.log'
        $pullProc = Start-Process -FilePath $ollamaExe -ArgumentList @('pull', $Model) `
            -WindowStyle Hidden -PassThru -Wait `
            -RedirectStandardOutput $pullOut -RedirectStandardError $pullErr

        if ($pullProc.ExitCode -ne 0) {
            Show-Failure "Failed to pull model $Model. See data\ollama_pull.err.log for details."
            exit 1
        }
        Write-Log "Model $Model pulled."
    }

    # 3. FRIDAY itself: skip if a tray/console instance is already running.
    $running = Get-CimInstance Win32_Process `
        -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -match [regex]::Escape('run.py') }

    if ($running) {
        $pids = ($running | Select-Object -ExpandProperty ProcessId) -join ', '
        Write-Log "FRIDAY already running (PID(s): $pids) - not starting a second instance."
    } else {
        Write-Log 'Starting FRIDAY via START.bat...'
        Start-Process -FilePath 'cmd.exe' -ArgumentList @('/c', "`"$Root\START.bat`"") -WindowStyle Hidden
        Write-Log 'FRIDAY start requested.'
    }

    Write-Log '--- launch finished ---'
} catch {
    Show-Failure "Unexpected launcher error: $($_.Exception.Message)"
    exit 1
}
