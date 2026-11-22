"""从 WSL 连接 Windows 默认浏览器；调试连接只通过本机管道转发。

Chrome 136 起不允许在默认用户目录上直接开启远程调试。素材流程因此会
把当前默认 Chrome 配置中的登录会话同步到一个独立的调试副本，再从 WSL
通过 CDP 连接副本。同步只读源配置，不关闭或修改用户正在使用的 Chrome。
"""

from __future__ import annotations

import base64
import json
import socket
import socketserver
import subprocess
import threading
from pathlib import Path


POWERSHELL = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")


def _powershell_command(script: str) -> list[str]:
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]


LAUNCH_SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$association = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice'
$commandKey = Get-Item -LiteralPath ('Registry::HKEY_CLASSES_ROOT\' + $association.ProgId + '\shell\open\command')
$command = $commandKey.GetValue('')
$match = [regex]::Match($command, '^"([^"]+)"')
if (-not $match.Success) { $match = [regex]::Match($command, '^(\S+\.exe)') }
if (-not $match.Success) { throw 'Cannot resolve the Windows default browser executable.' }
$executable = $match.Groups[1].Value
$name = [IO.Path]::GetFileName($executable).ToLowerInvariant()
if ($name -notin @('chrome.exe', 'msedge.exe', 'brave.exe', 'chromium.exe', 'vivaldi.exe')) {
    throw ('Windows default browser does not support this CDP workflow: ' + $name)
}

function Get-RunningBrowserProfile {
    param([string]$BrowserName, [string]$DefaultRoot)
    $best = $null
    try {
        $processes = @(Get-CimInstance Win32_Process -Filter ("Name='" + $BrowserName + "'") |
            Where-Object { $_.CommandLine -and $_.CommandLine -notmatch '--type=' })
        foreach ($process in $processes) {
            $line = [string]$process.CommandLine
            $userMatch = [regex]::Match($line, '--user-data-dir=(?:"([^"]+)"|([^\s]+))')
            $profileMatch = [regex]::Match($line, '--profile-directory=(?:"([^"]+)"|([^\s]+))')
            $userRoot = if ($userMatch.Success) {
                if ($userMatch.Groups[1].Success) { $userMatch.Groups[1].Value } else { $userMatch.Groups[2].Value }
            } else { $DefaultRoot }
            $profileName = if ($profileMatch.Success) {
                if ($profileMatch.Groups[1].Success) { $profileMatch.Groups[1].Value } else { $profileMatch.Groups[2].Value }
            } else { 'Default' }
            if ([IO.Directory]::Exists((Join-Path $userRoot $profileName))) {
                $candidate = [pscustomobject]@{ root = $userRoot; profile = $profileName; pid = $process.ProcessId }
                if ($null -eq $best -or ($candidate.root -eq $DefaultRoot -and $best.root -ne $DefaultRoot)) {
                    $best = $candidate
                }
            }
        }
    } catch { }
    return $best
}

$defaultRoot = if ($env:LOCALAPPDATA) {
    if ($name -eq 'msedge.exe') { Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\User Data' }
    else { Join-Path $env:LOCALAPPDATA 'Google\Chrome\User Data' }
} else { '' }
$source = Get-RunningBrowserProfile -BrowserName $name -DefaultRoot $defaultRoot
if ($null -eq $source) {
    $source = [pscustomobject]@{ root = $defaultRoot; profile = 'Default'; pid = 0 }
}

# Chrome 136+ 的远程调试要求非默认数据目录。使用当前用户配置的副本，
# 保留 Cookies、Local Storage 和验证状态；副本可在 Chrome 继续运行时使用。
$profileKey = ($source.profile -replace '[^A-Za-z0-9_-]', '_')
$profile = Join-Path $env:LOCALAPPDATA ('VideoInfo\WechatBrowser\profile-copy-' + $profileKey)
$profileDir = Join-Path $profile 'Default'
[IO.Directory]::CreateDirectory($profileDir) | Out-Null

function Copy-ProfileSnapshot {
    param([string]$SourceRoot, [string]$SourceProfile, [string]$TargetRoot, [string]$TargetProfile)
    if (-not [IO.Directory]::Exists($SourceProfile)) { return $false }
    [IO.Directory]::CreateDirectory($TargetProfile) | Out-Null
    $state = Join-Path $SourceRoot 'Local State'
    if ([IO.File]::Exists($state)) {
        Copy-Item -LiteralPath $state -Destination (Join-Path $TargetRoot 'Local State') -Force -ErrorAction SilentlyContinue
    }

    # 先同步非锁定文件和目录；Cookies 单独复制，失败时保留上一次可用副本。
    $files = @('Preferences', 'Secure Preferences', 'Web Data', 'Login Data', 'Login Data For Account', 'Bookmarks', 'Bookmarks.bak')
    foreach ($file in $files) {
        $srcFile = Join-Path $SourceProfile $file
        if ([IO.File]::Exists($srcFile)) {
            Copy-Item -LiteralPath $srcFile -Destination (Join-Path $TargetProfile $file) -Force -ErrorAction SilentlyContinue
        }
    }
    $dirs = @('Local Storage', 'Session Storage', 'IndexedDB', 'Service Worker', 'Storage', 'Extension State')
    foreach ($dir in $dirs) {
        $srcDir = Join-Path $SourceProfile $dir
        if ([IO.Directory]::Exists($srcDir)) {
            & robocopy $srcDir (Join-Path $TargetProfile $dir) /E /Z /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
        }
    }

    $sourceNetwork = Join-Path $SourceProfile 'Network'
    $targetNetwork = Join-Path $TargetProfile 'Network'
    if ([IO.Directory]::Exists($sourceNetwork)) {
        & robocopy $sourceNetwork $targetNetwork /E /Z /R:1 /W:1 /XF Cookies Cookies-journal Cookies-wal /NFL /NDL /NJH /NJS /NP | Out-Null
    }
    $cookieCopied = $false
    foreach ($cookieFile in @('Cookies', 'Cookies-journal', 'Cookies-wal')) {
        $srcCookie = Join-Path $sourceNetwork $cookieFile
        $dstCookie = Join-Path $targetNetwork $cookieFile
        if ([IO.File]::Exists($srcCookie)) {
            try {
                Copy-Item -LiteralPath $srcCookie -Destination $dstCookie -Force -ErrorAction Stop
                if ($cookieFile -eq 'Cookies') { $cookieCopied = $true }
            } catch { }
        }
    }
    return $cookieCopied
}

$sourceProfile = Join-Path $source.root $source.profile
$cookieCopied = Copy-ProfileSnapshot -SourceRoot $source.root -SourceProfile $sourceProfile -TargetRoot $profile -TargetProfile $profileDir
$existingCookies = Join-Path $profileDir 'Network\Cookies'
if (-not $cookieCopied -and -not [IO.File]::Exists($existingCookies)) {
    # 首次运行且 Chrome 正在锁定 Cookies 时，继续使用原有验证副本并明确报告原因。
    $fallbackProfile = Join-Path $env:LOCALAPPDATA ('VideoInfo\WechatBrowser\' + [IO.Path]::GetFileNameWithoutExtension($name))
    $profile = $fallbackProfile
    [IO.Directory]::CreateDirectory($profile) | Out-Null
    $profileDir = Join-Path $profile 'Default'
    $profileMode = 'isolated_fallback'
} else {
    $profileMode = if ($cookieCopied) { 'default_profile_copy' } else { 'cached_profile_copy' }
}

$activePortFile = Join-Path $profile 'DevToolsActivePort'
function Get-Endpoint {
    if (-not (Test-Path -LiteralPath $activePortFile)) { return $null }
    try {
        $lines = @(Get-Content -LiteralPath $activePortFile)
        if ($lines.Count -lt 2 -or $lines[1] -notmatch '^/devtools/browser/') { return $null }
        $port = [int]$lines[0]
        $request = [Net.WebRequest]::Create('http://127.0.0.1:' + $port + '/json/version')
        $request.Proxy = $null
        $request.Timeout = 1500
        $response = $request.GetResponse()
        try {
            $reader = [IO.StreamReader]::new($response.GetResponseStream())
            try { $version = $reader.ReadToEnd() | ConvertFrom-Json }
            finally { $reader.Dispose() }
        } finally { $response.Dispose() }
        if (([uri]$version.webSocketDebuggerUrl).AbsolutePath -ne $lines[1]) { return $null }
        return [pscustomobject]@{
            executable = $executable
            prog_id = $association.ProgId
            profile = $profile
            profile_source = $sourceProfile
            profile_name = $source.profile
            profile_mode = $profileMode
            cookie_copied = $cookieCopied
            port = $port
            websocket_path = $lines[1].ToString()
        }
    } catch { return $null }
}
$endpoint = Get-Endpoint
if ($null -eq $endpoint) {
    if (Test-Path -LiteralPath $activePortFile) { Remove-Item -LiteralPath $activePortFile }
    $browserArgs = @('--remote-debugging-port=0', ('--user-data-dir="' + $profile + '"'), '--no-first-run', '--no-default-browser-check', '--new-window', 'about:blank')
    Start-Process -FilePath $executable -ArgumentList $browserArgs | Out-Null
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 200
        $endpoint = Get-Endpoint
    } while ($null -eq $endpoint -and [DateTime]::UtcNow -lt $deadline)
}
if ($null -eq $endpoint) { throw 'Windows browser debugging endpoint did not become ready.' }
$endpoint | ConvertTo-Json -Compress
'''


class WindowsBrowser:
    """打开可见 Windows 默认浏览器，并向 Playwright 提供本机 CDP 地址。"""

    def __init__(self) -> None:
        self.info: dict = {}
        self.endpoint = ""
        self._server = None
        self._thread = None
        self._relays: set[subprocess.Popen] = set()
        self._lock = threading.Lock()

    def start(self) -> "WindowsBrowser":
        if not POWERSHELL.is_file():
            raise RuntimeError("无法从 WSL 调用 Windows PowerShell；不会回退到 WSL 浏览器")
        result = subprocess.run(
            _powershell_command(LAUNCH_SCRIPT),
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Windows 默认浏览器启动失败：{detail[:1000]}")
        self.info = json.loads(result.stdout.decode("utf-8-sig"))
        mode = self.info.get("profile_mode", "unknown")
        source = self.info.get("profile_source", "")
        if mode == "isolated_fallback":
            print(
                "⚠️ Windows 默认 Chrome 的 Cookies 当前被浏览器锁定，"
                "本次暂用隔离验证配置；关闭 Chrome 后下次会自动同步 Profile。",
                flush=True,
            )
        else:
            print(f"✅ 已同步 Windows Chrome 会话：{source}（{mode}）", flush=True)
        port = int(self.info["port"])
        if not 1 <= port <= 65535:
            raise RuntimeError("Windows 浏览器返回了无效的调试端口")
        websocket_path = self.info.get("websocket_path")
        if not isinstance(websocket_path, str) or not websocket_path.startswith("/devtools/browser/"):
            raise RuntimeError("Windows 浏览器返回了无效的 CDP 路径")
        bridge = self

        class RelayHandler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                bridge._relay(self.request, port)

        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), RelayHandler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        local_port = self._server.server_address[1]
        self.endpoint = f"ws://127.0.0.1:{local_port}{websocket_path}"
        return self

    def _relay(self, connection: socket.socket, windows_port: int) -> None:
        # 通过 WSL 的 .exe 标准输入/输出转发二进制 TCP；无需开放 Windows 防火墙。
        script = r'''
$ErrorActionPreference = 'Stop'
$client = [Net.Sockets.TcpClient]::new('127.0.0.1', WINDOWS_PORT)
try {
    $stream = $client.GetStream()
    $up = [Console]::OpenStandardInput().CopyToAsync($stream)
    $down = $stream.CopyToAsync([Console]::OpenStandardOutput())
    [Threading.Tasks.Task]::WaitAny([Threading.Tasks.Task[]]@($up, $down)) | Out-Null
} finally { $client.Dispose() }
'''.replace("WINDOWS_PORT", str(windows_port))
        process = subprocess.Popen(
            _powershell_command(script),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        with self._lock:
            self._relays.add(process)

        def upload() -> None:
            try:
                while data := connection.recv(64 * 1024):
                    pending = memoryview(data)
                    while pending:
                        written = process.stdin.write(pending)
                        if not written:
                            return
                        pending = pending[written:]
            except (OSError, ValueError):
                pass
            finally:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass

        threading.Thread(target=upload, daemon=True).start()
        try:
            while data := process.stdout.read(64 * 1024):
                connection.sendall(data)
        except OSError:
            pass
        finally:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._stop_relay(process)
            process.stdout.close()
            with self._lock:
                self._relays.discard(process)

    @staticmethod
    def _stop_relay(process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)

    def close(self) -> None:
        """只断开自动化连接，Windows 浏览器和验证状态继续保留。"""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        with self._lock:
            relays = list(self._relays)
        for process in relays:
            self._stop_relay(process)
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
