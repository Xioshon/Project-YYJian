param(
    [switch]$SelfTest,
    [switch]$NoCompile,
    [switch]$CheckOnly
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$LogDir = Join-Path $Root "workspace\logs"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$LogFile = Join-Path $LogDir ("startup_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))

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

try {
    Start-Transcript -Path $LogFile -Append | Out-Null

    Write-Host "YueYue Agent one-click launcher" -ForegroundColor Green
    Write-Host "Root: $Root"
    Write-Host "Log : $LogFile"

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
    Write-Host "Press Ctrl+C in this window to stop YueYue."
    Invoke-Python -Python $Python -Arguments @("main.py", "--telegram")
} catch {
    Write-Host ""
    Write-Host ("Launcher failed: " + $_.Exception.Message) -ForegroundColor Red
    exit 1
} finally {
    try {
        Stop-Transcript | Out-Null
    } catch {
    }
}
