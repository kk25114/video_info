"""从 WSL 连接 Windows 默认浏览器；调试连接只通过本机管道转发。"""

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
# Chrome 136+ 的远程调试要求非默认数据目录；固定目录可跨任务保存验证状态。
$profile = Join-Path $env:LOCALAPPDATA ('VideoInfo\WechatBrowser\' + [IO.Path]::GetFileNameWithoutExtension($name))
[IO.Directory]::CreateDirectory($profile) | Out-Null
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
