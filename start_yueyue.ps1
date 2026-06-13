param(
    [switch]$SelfTest,
    [switch]$NoCompile,
    [switch]$CheckOnly,
    [switch]$Restart
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$LogDir = Join-Path $Root "workspace\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("startup_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))
$ProjectCacheDir = Join-Path $Root "workspace\project_cache"
$LauncherPidFile = Join-Path $ProjectCacheDir "yueyue_launcher.pid"

function Write-Step {
    param([string]$Message)
    Write-Host ""
    Write-Host ("==> " + $Message) -ForegroundColor Cyan
}

function Resolve-Python {
    $candidates = @(
        @{ Command = "python"; Prefix = @() },
        @{ Command = "py"; Prefix = @("-3") }
    )
    foreach ($candidate in $candidates) {
        try {
            $output = & $candidate.Command @($candidate.Prefix) --version 2>&1
            if ($LASTEXITCODE -eq 0 -and $output) {
                return $candidate
            }
        } catch {
            continue
        }
    }
    throw "Python was not found. Install Python 3 or add it to PATH."
}

function Invoke-Python {
    param(
        [hashtable]$Python,
        [string[]]$Arguments
    )
    & $Python.Command @($Python.Prefix) @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Python command failed: $($Arguments -join ' ')"
    }
}

function Test-ProcessAlive {
    param([int]$ProcessId)
    if ($ProcessId -le 0) {
        return $false
    }
    try {
        $process = Get-Process -Id $ProcessId -ErrorAction Stop
        return -not $process.HasExited
    } catch {
        return $false
    }
}

function Read-LauncherPid {
    if (-not (Test-Path $LauncherPidFile)) {
        return 0
    }
    try {
        $raw = (Get-Content -LiteralPath $LauncherPidFile -ErrorAction Stop | Select-Object -First 1).Trim()
        $pidValue = 0
        if ([int]::TryParse($raw, [ref]$pidValue)) {
            return $pidValue
        }
    } catch {
    }
    return 0
}

function Clear-LauncherPid {
    try {
        Remove-Item -LiteralPath $LauncherPidFile -Force -ErrorAction SilentlyContinue
    } catch {
    }
}

function Stop-ProcessTree {
    param([int]$TargetPid)
    if ($TargetPid -le 0) {
        return
    }
    try {
        $children = Get-CimInstance Win32_Process -Filter "ParentProcessId=$TargetPid" -ErrorAction SilentlyContinue
        foreach ($child in $children) {
            Stop-ProcessTree -TargetPid ([int]$child.ProcessId)
        }
    } catch {
    }
    Stop-Process -Id $TargetPid -Force -ErrorAction SilentlyContinue
}

function Assert-SingleLauncher {
    $existingPid = Read-LauncherPid
    if (-not (Test-ProcessAlive -ProcessId $existingPid)) {
        Clear-LauncherPid
        return
    }
    if ($Restart) {
        Write-Host "Existing YueYue launcher detected (PID $existingPid). Restart requested; stopping it first." -ForegroundColor Yellow
        Stop-ProcessTree -TargetPid $existingPid
        Start-Sleep -Seconds 2
        Clear-LauncherPid
        return
    }
    throw "YueYue appears to be already running for this folder (launcher PID $existingPid). Use -Restart if you want to replace it."
}

function Write-LauncherPid {
    New-Item -ItemType Directory -Force -Path $ProjectCacheDir | Out-Null
    Set-Content -LiteralPath $LauncherPidFile -Value ([string]$PID) -Encoding ASCII
}

try {
    Start-Transcript -Path $LogFile -Append | Out-Null

    Write-Host "YueYue Agent one-click launcher" -ForegroundColor Green
    Write-Host "Root: $Root"
    Write-Host "Log : $LogFile"
    if (-not $CheckOnly) {
        Assert-SingleLauncher
    }

    Write-Step "Checking Python"
    $Python = Resolve-Python
    & $Python.Command @($Python.Prefix) --version

    Write-Step "Checking required files"
    $requiredFiles = @(
        "main.py",
        "core_agent.py",
        "core_tools.py",
        "agent_social.py",
        "agent_turns.py",
        "agent_latency.py",
        "agent_observability.py"
    )
    foreach ($file in $requiredFiles) {
        if (-not (Test-Path (Join-Path $Root $file))) {
            throw "Missing required file: $file"
        }
        Write-Host "ok $file"
    }

    Write-Step "Preparing workspace folders"
    $folders = @(
        "workspace",
        "workspace\assets",
        "workspace\assets\stickers",
        "workspace\history",
        "workspace\logs",
        "workspace\project_cache",
        "workspace\telegram_images",
        "workspace\tasks"
    )
    foreach ($folder in $folders) {
        New-Item -ItemType Directory -Force -Path (Join-Path $Root $folder) | Out-Null
        Write-Host "ok $folder"
    }

    Write-Step "Runtime health"
    Invoke-Python -Python $Python -Arguments @("main.py", "--health")

    if (-not $NoCompile) {
        Write-Step "Compiling core Python files"
        Invoke-Python -Python $Python -Arguments @(
            "-m", "py_compile",
            "core_tools.py",
            "core_agent.py",
            "main.py",
            "self_test.py",
            "agent_turns.py",
            "agent_session.py",
            "agent_context.py",
            "agent_memory.py",
            "agent_knowledge.py",
            "agent_eval.py",
            "agent_task_graph.py",
            "agent_worker.py",
            "agent_planner.py",
            "agent_replay.py",
        "agent_subagents.py",
        "agent_verification.py",
        "agent_action_verification.py",
        "agent_transactions.py",
        "agent_social.py",
        "agent_observability.py"
    )
    }

    if ($SelfTest) {
        Write-Step "Running full self-test"
        Invoke-Python -Python $Python -Arguments @("self_test.py")
    } else {
        Write-Host ""
        Write-Host "Tip: run start_yueyue.bat -SelfTest when you want the full regression suite before startup." -ForegroundColor DarkGray
    }

    if ($CheckOnly) {
        Write-Step "Check-only mode complete"
        Write-Host "All startup checks passed. Telegram bot was not started."
        exit 0
    }

    Write-Step "Starting Telegram bot"
    Write-LauncherPid
    Write-Host "Press Ctrl+C in this window to stop YueYue."
    Invoke-Python -Python $Python -Arguments @("main.py", "--telegram")
} catch {
    Write-Host ""
    Write-Host ("Launcher failed: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
} finally {
    if (-not $CheckOnly) {
        $existingPid = Read-LauncherPid
        if ($existingPid -eq $PID) {
            Clear-LauncherPid
        }
    }
    try {
        Stop-Transcript | Out-Null
    } catch {
    }
}
