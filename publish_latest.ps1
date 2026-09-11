<#
.SYNOPSIS
    在 Windows 上发布最新视频到小红书创作服务平台。

.DESCRIPTION
    默认视频目录优先沿用旧 WSL 流程对应的 D:\Program Files\下载；
    若该目录不存在，则使用当前 Windows 用户的 Downloads 目录。
    默认复用当前 Windows Chrome 的 User Data/Default 配置，因此脚本打开的
    是平时已经登录小红书的浏览器账号。若 Chrome 正在运行，请先关闭全部
    Chrome 窗口；也可以通过 -ChromeUserDataDir/-ChromeProfileDirectory 指定配置。
#>
[CmdletBinding()]
param(
    [string]$Video,
    [string]$VideoDir,
    [string]$Title,
    [string]$Description,
    [string]$Python,
    [string]$BrowserPath,
    [string]$ChromeUserDataDir,
    [string]$ChromeProfileDirectory,
    [switch]$NoAutoDesc,
    [switch]$NoPublish,
    [switch]$PauseBeforePublish,
    [switch]$SaveState,
    [int]$SlowMo = 100
)

$ErrorActionPreference = "Stop"

function Get-DefaultVideoDir {
    if ($env:VIDEO_INFO_VIDEO_DIR) {
        return $env:VIDEO_INFO_VIDEO_DIR
    }

    # 兼容旧 WSL 脚本中的 /mnt/d/Program Files/下载。
    $legacyDir = "D:\Program Files\下载"
    if (Test-Path -LiteralPath $legacyDir -PathType Container) {
        return $legacyDir
    }

    return (Join-Path ([Environment]::GetFolderPath("UserProfile")) "Downloads")
}

function Resolve-PythonLauncher {
    param([string]$RequestedPython, [string]$Root)

    if ($RequestedPython) {
        if (-not (Test-Path -LiteralPath $RequestedPython -PathType Leaf)) {
            throw "指定的 Python 不存在：$RequestedPython"
        }
        return @{ Command = (Resolve-Path -LiteralPath $RequestedPython).Path; Prefix = @() }
    }

    $venvPython = Join-Path $Root ".venv\Scripts\python.exe"
    if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
        return @{ Command = $venvPython; Prefix = @() }
    }

    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        return @{ Command = $py.Path; Prefix = @("-3") }
    }

    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    }
    if ($pythonCommand) {
        return @{ Command = $pythonCommand.Path; Prefix = @() }
    }

    throw "未找到 Python 3。请安装 Python 3.10+，或通过 -Python 指定 python.exe。"
}

function Get-DefaultChromeUserDataDir {
    param([string]$Root)

    if ($env:VIDEO_INFO_CHROME_USER_DATA_DIR) {
        return $env:VIDEO_INFO_CHROME_USER_DATA_DIR
    }

    if ($env:LOCALAPPDATA) {
        $chromeDir = Join-Path $env:LOCALAPPDATA "Google\Chrome\User Data"
        if (Test-Path -LiteralPath $chromeDir -PathType Container) {
            return $chromeDir
        }

        $edgeDir = Join-Path $env:LOCALAPPDATA "Microsoft\Edge\User Data"
        if (Test-Path -LiteralPath $edgeDir -PathType Container) {
            return $edgeDir
        }
    }

    # 浏览器目录不存在时保留隔离目录作为最后的兼容回退。
    return (Join-Path $Root "xiaohongshu_playwright\browser_profile")
}

$repoRoot = Split-Path -Parent $PSCommandPath
$publisher = Join-Path $repoRoot "xiaohongshu_playwright\publish_video.py"
if (-not (Test-Path -LiteralPath $publisher -PathType Leaf)) {
    throw "未找到小红书发布器：$publisher"
}

if (-not $ChromeUserDataDir) {
    $ChromeUserDataDir = Get-DefaultChromeUserDataDir -Root $repoRoot
}
if (-not $ChromeProfileDirectory) {
    if ($env:VIDEO_INFO_CHROME_PROFILE_DIRECTORY) {
        $ChromeProfileDirectory = $env:VIDEO_INFO_CHROME_PROFILE_DIRECTORY
    } else {
        $ChromeProfileDirectory = "Default"
    }
}

if ($Video) {
    if (-not (Test-Path -LiteralPath $Video -PathType Leaf)) {
        throw "指定视频不存在：$Video"
    }
    $latestVideo = Get-Item -LiteralPath $Video
} else {
    if (-not $VideoDir) {
        $VideoDir = Get-DefaultVideoDir
    }
    if (-not (Test-Path -LiteralPath $VideoDir -PathType Container)) {
        throw "视频目录不存在：$VideoDir`n可通过 -VideoDir 指定目录，或设置环境变量 VIDEO_INFO_VIDEO_DIR。"
    }

    $latestVideo = Get-ChildItem -LiteralPath $VideoDir -File -Filter "*.mp4" |
        Sort-Object -Property LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $latestVideo) {
        throw "在目录中未找到 mp4 视频：$VideoDir"
    }
}

$launcher = Resolve-PythonLauncher -RequestedPython $Python -Root $repoRoot
$statePath = Join-Path $repoRoot "xiaohongshu_playwright\storage_state.json"
$screenshotPath = Join-Path $repoRoot "xiaohongshu_playwright\test\latest.png"

$publishArgs = @(
    $publisher,
    "--video", $latestVideo.FullName,
    "--user-data-dir", $ChromeUserDataDir,
    "--profile-directory", $ChromeProfileDirectory,
    "--screenshot", $screenshotPath,
    "--slow-mo", $SlowMo
)

if ($SaveState) {
    $publishArgs += @("--state-path", $statePath, "--save-state")
}

if (-not $NoAutoDesc) {
    $publishArgs += "--auto-desc"
}
if ($Title) {
    $publishArgs += @("--title", $Title)
}
if ($Description) {
    $publishArgs += @("--desc", $Description)
}
if ($BrowserPath) {
    $publishArgs += @("--browser-path", $BrowserPath)
}
if ($NoPublish) {
    $publishArgs += "--no-publish"
}
if ($PauseBeforePublish) {
    $publishArgs += "--pause-before-publish"
}

Write-Host "使用最新视频：$($latestVideo.FullName)" -ForegroundColor Cyan
Write-Host "Chrome 配置：$ChromeUserDataDir ($ChromeProfileDirectory)" -ForegroundColor DarkCyan
if ($SaveState) {
    Write-Host "登录态备份：$statePath" -ForegroundColor DarkCyan
} else {
    Write-Host "登录来源：Chrome 用户配置（不另存 Cookie）" -ForegroundColor DarkCyan
}

$commandArgs = @($launcher.Prefix) + $publishArgs
& $launcher.Command @commandArgs
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
