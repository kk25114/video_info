"""用 Windows 默认浏览器搜索公众号，右键保存正文中的图片。"""

from __future__ import annotations

import base64
import json
import subprocess
from pathlib import Path
from typing import Any


POWERSHELL = Path("/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe")


def _powershell_command(script: str) -> tuple[list[str], bytes]:
    # Windows limits process command lines to 32,767 characters. Send the
    # encoded script over stdin so the full UI Automation program fits.
    encoded = base64.b64encode(script.encode("utf-8"))
    bootstrap = (
        "$encoded = [Console]::In.ReadToEnd(); "
        "$script = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded)); "
        "Invoke-Expression $script"
    )
    command = [str(POWERSHELL), "-NoProfile", "-Sta", "-NonInteractive", "-Command", bootstrap]
    return command, encoded


SCRIPT = r'''
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
Add-Type -AssemblyName UIAutomationClient

$payloadBytes = [Convert]::FromBase64String('__PAYLOAD__')
$payload = [Text.Encoding]::UTF8.GetString($payloadBytes) | ConvertFrom-Json

$source = @'
using System;
using System.Runtime.InteropServices;
public static class VideoInfoWindow {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr hWnd, out uint processId);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int command);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint attach, uint attachTo, bool attachState);
    [DllImport("user32.dll")] public static extern IntPtr SetThreadDpiAwarenessContext(IntPtr context);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
    [DllImport("user32.dll")] public static extern void mouse_event(uint flags, uint dx, uint dy, uint data, UIntPtr extra);
    [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
}
'@
Add-Type $source
[void][VideoInfoWindow]::SetThreadDpiAwarenessContext([IntPtr](-4))

function Activate-Window([IntPtr]$handle) {
    [uint32]$targetPid = 0
    [void][VideoInfoWindow]::GetWindowThreadProcessId($handle, [ref]$targetPid)
    $shell = New-Object -ComObject WScript.Shell
    [void]$shell.AppActivate([int]$targetPid)
    $foreground = [VideoInfoWindow]::GetForegroundWindow()
    [uint32]$ignoredPid = 0
    $foregroundThread = [VideoInfoWindow]::GetWindowThreadProcessId($foreground, [ref]$ignoredPid)
    $targetThread = [VideoInfoWindow]::GetWindowThreadProcessId($handle, [ref]$ignoredPid)
    $currentThread = [VideoInfoWindow]::GetCurrentThreadId()
    $attached = @()
    try {
        $threadIds = @($foregroundThread, $targetThread) | Select-Object -Unique
        foreach ($threadId in $threadIds) {
            if ($threadId -and $threadId -ne $currentThread) {
                if ([VideoInfoWindow]::AttachThreadInput($currentThread, [uint32]$threadId, $true)) {
                    $attached += [uint32]$threadId
                }
            }
        }
        [void][VideoInfoWindow]::ShowWindow($handle, 9)
        [void][VideoInfoWindow]::BringWindowToTop($handle)
        $activated = [VideoInfoWindow]::SetForegroundWindow($handle)
        if (-not $activated) {
            try { [System.Windows.Automation.AutomationElement]::FromHandle($handle).SetFocus() } catch { }
        }
        return [VideoInfoWindow]::GetForegroundWindow() -eq $handle
    } finally {
        foreach ($threadId in $attached) {
            [void][VideoInfoWindow]::AttachThreadInput($currentThread, $threadId, $false)
        }
    }
}

function Get-BrowserInfo {
    $association = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice'
    $commandKey = Get-Item -LiteralPath ('Registry::HKEY_CLASSES_ROOT\' + $association.ProgId + '\shell\open\command')
    $command = $commandKey.GetValue('')
    $match = [regex]::Match($command, '^"([^"]+)"')
    if (-not $match.Success) { $match = [regex]::Match($command, '^(\S+\.exe)') }
    if (-not $match.Success) { throw '无法解析 Windows 默认浏览器程序。' }
    $executable = $match.Groups[1].Value
    $name = [IO.Path]::GetFileName($executable).ToLowerInvariant()
    $browser = switch ($name) {
        'chrome.exe' { 'chrome' }
        'msedge.exe' { 'msedge' }
        'brave.exe' { 'brave' }
        'vivaldi.exe' { 'vivaldi' }
        'chromium.exe' { 'chromium' }
        default { throw ('默认浏览器不支持公众号素材流程：' + $name) }
    }
    $relativeRoot = switch ($browser) {
        'chrome' { 'Google\Chrome\User Data' }
        'msedge' { 'Microsoft\Edge\User Data' }
        'brave' { 'BraveSoftware\Brave-Browser\User Data' }
        'vivaldi' { 'Vivaldi\User Data' }
        'chromium' { 'Chromium\User Data' }
    }
    $defaultRoot = Join-Path $env:LOCALAPPDATA $relativeRoot
    $profileName = ''
    $profileProcessId = 0
    $processes = @(Get-CimInstance Win32_Process -Filter ("Name='" + $name + "'") |
        Where-Object { $_.CommandLine -and $_.CommandLine -notmatch '--type=' })
    foreach ($process in $processes) {
        $line = [string]$process.CommandLine
        $rootMatch = [regex]::Match($line, '--user-data-dir=(?:"([^"]+)"|([^\s]+))')
        $root = if ($rootMatch.Success) {
            if ($rootMatch.Groups[1].Success) { $rootMatch.Groups[1].Value } else { $rootMatch.Groups[2].Value }
        } else { $defaultRoot }
        if ([IO.Path]::GetFullPath($root).TrimEnd('\') -ne [IO.Path]::GetFullPath($defaultRoot).TrimEnd('\')) { continue }
        $profileMatch = [regex]::Match($line, '--profile-directory=(?:"([^"]+)"|([^\s]+))')
        $candidate = if ($profileMatch.Success) {
            if ($profileMatch.Groups[1].Success) { $profileMatch.Groups[1].Value } else { $profileMatch.Groups[2].Value }
        } else { 'Default' }
        if (-not [IO.Directory]::Exists((Join-Path $defaultRoot $candidate))) { continue }
        $profileName = $candidate
        $profileProcessId = [int]$process.ProcessId
        break
    }
    if (-not $profileName) {
        $profileName = 'Default'
        $statePath = Join-Path $defaultRoot 'Local State'
        if ([IO.File]::Exists($statePath)) {
            try {
                $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
                if ($state.profile.last_used) { $profileName = [string]$state.profile.last_used }
            } catch { }
        }
    }
    if (-not [IO.Directory]::Exists((Join-Path $defaultRoot $profileName))) {
        throw ('默认浏览器用户配置不存在：' + $profileName)
    }
    return [pscustomobject]@{
        executable = $executable
        process_name = $name
        profile = $profileName
        user_data_dir = $defaultRoot
        profile_process_id = $profileProcessId
    }
}

function Get-TargetWindows($browserInfo) {
    $windows = [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
        [System.Windows.Automation.TreeScope]::Children,
        [System.Windows.Automation.Condition]::TrueCondition
    )
    $found = @()
    for ($i = 0; $i -lt $windows.Count; $i++) {
        $window = $windows.Item($i)
        if ($window.Current.ClassName -notlike 'Chrome_WidgetWin_*') { continue }
        $process = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $window.Current.ProcessId) -ErrorAction SilentlyContinue
        if (-not $process -or $process.Name -ne $browserInfo.process_name) { continue }
        $line = [string]$process.CommandLine
        $rootMatch = [regex]::Match($line, '--user-data-dir=(?:"([^"]+)"|([^\s]+))')
        $root = if ($rootMatch.Success) {
            if ($rootMatch.Groups[1].Success) { $rootMatch.Groups[1].Value } else { $rootMatch.Groups[2].Value }
        } else { $browserInfo.user_data_dir }
        if ([IO.Path]::GetFullPath($root).TrimEnd('\') -ne [IO.Path]::GetFullPath($browserInfo.user_data_dir).TrimEnd('\')) { continue }
        $profileMatch = [regex]::Match($line, '--profile-directory=(?:"([^"]+)"|([^\s]+))')
        $profile = if ($profileMatch.Success) {
            if ($profileMatch.Groups[1].Success) { $profileMatch.Groups[1].Value } else { $profileMatch.Groups[2].Value }
        } else { 'Default' }
        if ($profile -ne $browserInfo.profile) { continue }
        $found += $window
    }
    return ,$found
}

function Get-ActiveDocument($window) {
    $handle = [IntPtr]$window.Current.NativeWindowHandle
    if ([VideoInfoWindow]::GetForegroundWindow() -ne $handle) {
        [void](Activate-Window $handle)
    }
    if ([VideoInfoWindow]::GetForegroundWindow() -ne $handle) {
        throw '无法激活 Windows 浏览器，请确认桌面已解锁并保持可操作。'
    }
    # Chrome can expose stale document trees from inactive tabs, even with
    # IsOffscreen=false. Anchor the tree to the page actually under the cursor.
    $bounds = $window.Current.BoundingRectangle
    $point = New-Object System.Windows.Point(
        ($bounds.Left + $bounds.Width / 2), ($bounds.Top + $bounds.Height / 2))
    $element = [System.Windows.Automation.AutomationElement]::FromPoint($point)
    for ($depth = 0; $depth -lt 30 -and $null -ne $element; $depth++) {
        if ($element.Current.ControlType -eq [System.Windows.Automation.ControlType]::Document -and
            $element.Current.AutomationId -eq 'RootWebArea') { return $element }
        $element = [System.Windows.Automation.TreeWalker]::ControlViewWalker.GetParent($element)
    }
    return $null
}

function Get-PageRecords($browserInfo) {
    $records = @()
    foreach ($window in (Get-TargetWindows $browserInfo)) {
        try {
            $document = Get-ActiveDocument $window
            if ($null -eq $document) { continue }
            $address = [string]$document.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value
            $text = [string]$document.GetCurrentPattern([System.Windows.Automation.TextPattern]::Pattern).DocumentRange.GetText(200000)
            $children = $document.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                [System.Windows.Automation.Condition]::TrueCondition)
            $links = @()
            $body = $null
            $articleTitle = ''
            foreach ($child in $children) {
                if ($child.Current.AutomationId -eq 'js_content') { $body = $child }
                if ($child.Current.AutomationId -eq 'activity-name') { $articleTitle = [string]$child.Current.Name }
                if ($child.Current.ControlType -ne [System.Windows.Automation.ControlType]::Hyperlink -or
                    $child.Current.AutomationId -notmatch '^sogou_vr_.*_title_\d+$') { continue }
                $href = [string]$child.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).Current.Value
                if ($href) { $links += [pscustomobject]@{ title = [string]$child.Current.Name; url = $href } }
            }
            $images = @()
            if ($null -ne $body) {
                $bodyImages = $body.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                    (New-Object System.Windows.Automation.PropertyCondition(
                        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                        [System.Windows.Automation.ControlType]::Image)))
                for ($j = 0; $j -lt $bodyImages.Count; $j++) {
                    $bounds = $bodyImages.Item($j).Current.BoundingRectangle
                    if ($bounds.Width -ge 320 -and $bounds.Height -ge 180 -and
                        [Math]::Max($bounds.Width, $bounds.Height) / [Math]::Min($bounds.Width, $bounds.Height) -le 4.5) {
                        $images += [pscustomobject]@{ index = $j + 1; width = $bounds.Width; height = $bounds.Height }
                    }
                }
            }
            $records += [pscustomobject]@{
                window_handle = [long]$window.Current.NativeWindowHandle
                window_pid = [int]$window.Current.ProcessId
                document_title = [string]$document.Current.Name
                article_title = $articleTitle
                has_body = $null -ne $body
                address = $address
                images = $images
                text = $text
                links = $links
            }
        } catch [System.Windows.Automation.ElementNotAvailableException] {
            # Navigation replaces UIA elements; the caller polls a fresh tree.
            continue
        }
    }
    return ,$records
}

function Start-ProfileTab($browserInfo, [string]$url) {
    $quotedUrl = '"' + $url.Replace('"', '\"') + '"'
    $arguments = @('--profile-directory="' + $browserInfo.profile + '"', '--new-tab', $quotedUrl)
    Start-Process -FilePath $browserInfo.executable -ArgumentList $arguments | Out-Null
}

function Get-SaveDialog($window) {
    try {
        $dialogs = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants,
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ClassNameProperty, '#32770')))
        foreach ($dialog in $dialogs) {
            if ($dialog.Current.Name -in @('另存为', 'Save As')) { return $dialog }
        }
    } catch [System.Windows.Automation.ElementNotAvailableException] { }
    return $null
}

function Find-DialogControl($dialog, [string]$id, $type) {
    return $dialog.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
        (New-Object System.Windows.Automation.AndCondition(
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::AutomationIdProperty, $id)),
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty, $type)))))
}

function Normalize-Title([string]$title) { return ($title -replace '[\s\p{P}\p{S}]', '') }

try {
$browserInfo = Get-BrowserInfo
$result = $null
switch ([string]$payload.action) {
    'probe' {
        $result = $browserInfo
    }
    'search' {
        $url = 'https://weixin.sogou.com/weixin?type=2&query=' + [Uri]::EscapeDataString([string]$payload.query)
        Start-ProfileTab $browserInfo $url
        $deadline = [DateTime]::UtcNow.AddSeconds(35)
        $page = $null
        do {
            Start-Sleep -Milliseconds 350
            foreach ($candidate in (Get-PageRecords $browserInfo)) {
                if ($candidate.address -match '^https://weixin\.sogou\.com/weixin' -and
                    $candidate.document_title -match '搜狗微信搜索' -and
                    $candidate.text.Contains([string]$payload.query) -and
                    ($candidate.links.Count -gt 0 -or $candidate.text -match '没有找到|未找到|没有相关')) {
                    $page = $candidate; break
                }
            }
        } while ($null -eq $page -and [DateTime]::UtcNow -lt $deadline)
        if ($null -eq $page) { throw 'Windows 默认浏览器没有返回搜狗微信搜索结果。' }
        $lines = @($page.text -split "`r?`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ })
        $results = @()
        foreach ($link in $page.links) {
            $lineIndex = -1
            for ($i = 0; $i -lt $lines.Count; $i++) {
                if ($lines[$i] -eq $link.title -or $lines[$i].StartsWith($link.title.Substring(0, [Math]::Min($link.title.Length, 40)))) {
                    $lineIndex = $i; break
                }
            }
            $description = if ($lineIndex -ge 0 -and $lineIndex + 1 -lt $lines.Count) { $lines[$lineIndex + 1] } else { '' }
            $publisherLine = if ($lineIndex -ge 0 -and $lineIndex + 2 -lt $lines.Count) { $lines[$lineIndex + 2] } else { '' }
            $results += [pscustomobject]@{
                query = [string]$payload.query
                title = $link.title
                description = $description
                creator = $publisherLine
                keywords = [string]$payload.query
                page_url = $link.url
                article_url = ''
                article_status = 'resolve_in_windows_browser'
                search_url = $url
                image_url = ''
                thumbnail_url = ''
                source = 'wechat'
                license = 'unknown'
            }
            if ($results.Count -ge [int]$payload.limit) { break }
        }
        $result = [pscustomobject]@{ results = $results; page_title = $page.document_title; address = $page.address }
    }
    'open_article' {
        Start-ProfileTab $browserInfo ([string]$payload.url)
        $deadline = [DateTime]::UtcNow.AddSeconds(40)
        $page = $null
        $expectedTitle = Normalize-Title ([string]$payload.title)
        $titlePrefix = $expectedTitle.Substring(0, [Math]::Min($expectedTitle.Length, 30))
        do {
            Start-Sleep -Milliseconds 400
            $pages = Get-PageRecords $browserInfo
            foreach ($candidate in $pages) {
                $actualTitle = Normalize-Title ([string]$candidate.article_title)
                if ($candidate.address -match '^https://mp\.weixin\.qq\.com/' -and
                    $candidate.has_body -and $candidate.article_title -and
                    $actualTitle.Contains($titlePrefix)) {
                    $page = $candidate; break
                }
                if (-not $candidate.has_body -and
                    $candidate.address -match '^https://mp\.weixin\.qq\.com/' -and
                    $candidate.text -match '环境异常|完成验证|去验证') { $page = $candidate; break }
            }
        } while ($null -eq $page -and [DateTime]::UtcNow -lt $deadline)
        if ($null -eq $page) { throw 'Windows 默认浏览器没有加载目标公众号文章。' }
        $status = if (-not $page.has_body -and
            ($page.address -match 'wappoc_appmsgcaptcha' -or $page.text -match '环境异常|完成验证|去验证')) {
            'verification_required'
        } elseif ($page.has_body) {
            'ok'
        } else { 'article_body_not_found' }
        $result = [pscustomobject]@{
            status = $status
            title = $page.article_title
            address = $page.address
            window_handle = $page.window_handle
            window_pid = $page.window_pid
            images = $page.images
        }
    }
    'save_image' {
        $page = $null
        foreach ($candidate in (Get-PageRecords $browserInfo)) {
            if ($candidate.window_handle -eq [long]$payload.page.window_handle -and
                $candidate.address -eq [string]$payload.page.address -and $candidate.has_body) {
                $page = $candidate; break
            }
        }
        if ($null -eq $page) { throw '目标文章已切换或关闭，无法保存该文中的图片。' }
        $windowHandle = [IntPtr]$page.window_handle
        $window = [System.Windows.Automation.AutomationElement]::FromHandle($windowHandle)
        if ($null -ne (Get-SaveDialog $window)) { throw '目标浏览器已有保存窗口，请先完成或取消该操作。' }
        [void](Activate-Window $windowHandle)
        Start-Sleep -Milliseconds 200
        if ([VideoInfoWindow]::GetForegroundWindow() -ne $windowHandle) {
            throw '目标文章窗口未能切换到前台。'
        }
        $document = Get-ActiveDocument $window
        $body = $document.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'js_content')))
        $images = $body.FindAll([System.Windows.Automation.TreeScope]::Descendants,
            (New-Object System.Windows.Automation.PropertyCondition(
                [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                [System.Windows.Automation.ControlType]::Image)))
        $imageIndex = [int]$payload.image_index
        if ($imageIndex -lt 1 -or $imageIndex -gt $images.Count) { throw '正文图片序号已失效。' }
        $image = $images.Item($imageIndex - 1)
        $image.GetCurrentPattern([System.Windows.Automation.ScrollItemPattern]::Pattern).ScrollIntoView()
        $imageVisible = $false
        for ($attempt = 0; $attempt -lt 6; $attempt++) {
            Start-Sleep -Milliseconds 400
            # Lazy loading can change the layout after ScrollIntoView. Refresh
            # the active tree and wait until the selected image is hit-testable.
            $document = Get-ActiveDocument $window
            $body = $document.FindFirst([System.Windows.Automation.TreeScope]::Descendants,
                (New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'js_content')))
            $images = $body.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                (New-Object System.Windows.Automation.PropertyCondition(
                    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                    [System.Windows.Automation.ControlType]::Image)))
            $image = $images.Item($imageIndex - 1)
            $bounds = $image.Current.BoundingRectangle
            $viewport = $document.Current.BoundingRectangle
            $left = [Math]::Max($bounds.Left, $viewport.Left)
            $right = [Math]::Min($bounds.Right, $viewport.Right)
            $top = [Math]::Max($bounds.Top, $viewport.Top)
            $bottom = [Math]::Min($bounds.Bottom, $viewport.Bottom - 100)
            if ($right -gt $left -and $bottom -gt $top) {
                $x = [int](($left + $right) / 2)
                $y = [int](($top + $bottom) / 2)
                $hit = [System.Windows.Automation.AutomationElement]::FromPoint(
                    (New-Object System.Windows.Point($x, $y)))
                if (($hit.GetRuntimeId() -join ',') -eq ($image.GetRuntimeId() -join ',')) {
                    $imageVisible = $true; break
                }
            }
            $scroll = $document.GetCurrentPattern([System.Windows.Automation.ScrollPattern]::Pattern)
            $viewSize = $scroll.Current.VerticalViewSize
            if ($scroll.Current.VerticallyScrollable -and $viewSize -gt 0 -and $viewSize -lt 100) {
                $scrollRange = $viewport.Height * (100 / $viewSize - 1)
                $targetPercent = $scroll.Current.VerticalScrollPercent +
                    100 * ($bounds.Top - $viewport.Top - 40) / $scrollRange
                $scroll.SetScrollPercent(-1, [Math]::Min(100, [Math]::Max(0, $targetPercent)))
            }
        }
        if (-not $imageVisible) { throw ('正文第 ' + $imageIndex + ' 张图片尚未可见或被遮挡。') }
        [void][VideoInfoWindow]::SetCursorPos($x, $y)
        [VideoInfoWindow]::mouse_event(8, 0, 0, 0, [UIntPtr]::Zero)
        [VideoInfoWindow]::mouse_event(16, 0, 0, 0, [UIntPtr]::Zero)
        $saveItem = $null
        $deadline = [DateTime]::UtcNow.AddSeconds(5)
        do {
            Start-Sleep -Milliseconds 200
            $roots = [System.Windows.Automation.AutomationElement]::RootElement.FindAll(
                [System.Windows.Automation.TreeScope]::Children, [System.Windows.Automation.Condition]::TrueCondition)
            foreach ($root in $roots) {
                if ($root.Current.ProcessId -ne $page.window_pid) { continue }
                $items = $root.FindAll([System.Windows.Automation.TreeScope]::Descendants,
                    (New-Object System.Windows.Automation.PropertyCondition(
                        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
                        [System.Windows.Automation.ControlType]::MenuItem)))
                foreach ($item in $items) {
                    if ($item.Current.Name -match '^(图片另存为|Save image as)') { $saveItem = $item; break }
                }
                if ($null -ne $saveItem) { break }
            }
        } while ($null -eq $saveItem -and [DateTime]::UtcNow -lt $deadline)
        if ($null -eq $saveItem) { throw '没有找到“图片另存为”菜单。' }
        $saveItem.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
        $dialog = $null
        $saveDirectory = $null
        $saved = $false
        try {
            $filename = $null
            $deadline = [DateTime]::UtcNow.AddSeconds(15)
            do {
                Start-Sleep -Milliseconds 250
                $dialog = Get-SaveDialog $window
                if ($null -ne $dialog) {
                    $filename = Find-DialogControl $dialog '1001' ([System.Windows.Automation.ControlType]::Edit)
                }
            } while ($null -eq $filename -and [DateTime]::UtcNow -lt $deadline)
            if ($null -eq $filename) { throw '图片保存窗口未准备好。' }
            $value = $filename.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern)
            $extension = [IO.Path]::GetExtension([string]$value.Current.Value)
            if ($extension -notmatch '^\.(jpe?g|jfif|png|webp|gif|avif|bmp)$') {
                throw ('图片保存窗口返回了不支持的格式：' + $extension)
            }
            $saveDirectory = Join-Path $env:TEMP ('VideoInfoWechatImages-' + [Guid]::NewGuid().ToString('N'))
            [void][IO.Directory]::CreateDirectory($saveDirectory)
            $imagePath = Join-Path $saveDirectory ('body-image-' + $imageIndex + $extension)
            $value.SetValue($imagePath)
            if ($value.Current.Value -ne $imagePath) { throw '图片保存路径校验失败。' }
            $button = Find-DialogControl $dialog '1' ([System.Windows.Automation.ControlType]::Button)
            $button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
            $deadline = [DateTime]::UtcNow.AddSeconds(30)
            $previousSize = -1
            $stable = 0
            do {
                Start-Sleep -Milliseconds 350
                if ([IO.File]::Exists($imagePath)) {
                    $size = (Get-Item -LiteralPath $imagePath).Length
                    if ($size -gt 0 -and $size -eq $previousSize) { $stable++ } else { $stable = 0 }
                    $previousSize = $size
                }
            } while ($stable -lt 2 -and [DateTime]::UtcNow -lt $deadline)
            if ($stable -lt 2) { throw '图片下载未完成。' }
            $saved = $true
            $result = [pscustomobject]@{ file_path = $imagePath; image_index = $imageIndex; bytes = $previousSize }
        } finally {
            if (-not $saved) {
                if ($null -ne $dialog) {
                    try {
                        $cancel = Find-DialogControl $dialog '2' ([System.Windows.Automation.ControlType]::Button)
                        if ($null -ne $cancel) { $cancel.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
                    } catch { }
                }
                if ($saveDirectory -and [IO.Directory]::Exists($saveDirectory)) {
                    Remove-Item -LiteralPath $saveDirectory -Recurse -Force -ErrorAction SilentlyContinue
                }
            }
        }
    }
    default { throw ('不支持的 Windows 浏览器操作：' + $payload.action) }
}

$result | ConvertTo-Json -Depth 8 -Compress
} catch {
    [pscustomobject]@{ error = $_.Exception.Message } | ConvertTo-Json -Compress
    exit 1
}
'''


class WindowsBrowser:
    """通过 Windows UI Automation 操作已登录的默认浏览器。"""

    def __init__(self) -> None:
        self.info: dict[str, Any] = {}
        self._started = False
        self._article_page: dict[str, Any] = {}

    def _request(self, action: str, **payload: Any) -> dict[str, Any]:
        if not POWERSHELL.is_file():
            raise RuntimeError("无法从 WSL 调用 Windows PowerShell")
        document = {"action": action, **payload}
        encoded_payload = base64.b64encode(
            json.dumps(document, ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        script = SCRIPT.replace("__PAYLOAD__", encoded_payload)
        command, script_input = _powershell_command(script)
        result = subprocess.run(
            command,
            input=script_input,
            capture_output=True,
            timeout=75,
            check=False,
        )
        stdout = result.stdout.decode("utf-8-sig", errors="replace").strip()
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as exc:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Windows 默认浏览器自动化失败：{detail[:500] or stdout[:500]}") from exc
        if not isinstance(data, dict):
            raise RuntimeError("Windows 默认浏览器返回了无效结果")
        if result.returncode or data.get("error"):
            raise RuntimeError(str(data.get("error") or "Windows 默认浏览器自动化失败"))
        return data

    def start(self) -> "WindowsBrowser":
        if self._started:
            return self
        self.info = self._request("probe")
        self._started = True
        print(
            f"🌐 Windows 默认浏览器：{self.info.get('executable')}"
            f"（配置 {self.info.get('profile')}）",
            flush=True,
        )
        return self

    def search_wechat(self, query: str, limit: int) -> list[dict[str, str]]:
        if not self._started:
            self.start()
        result = self._request("search", query=query, limit=max(1, int(limit)))
        return [item for item in result.get("results", []) if isinstance(item, dict)]

    def open_article(self, url: str, title: str) -> dict[str, Any]:
        if not self._started:
            self.start()
        self._article_page = {}
        page = self._request("open_article", url=url, title=title)
        if page.get("status") == "ok":
            self._article_page = page
        return page

    def save_image(self, image_index: int) -> dict[str, Any]:
        if not self._article_page:
            raise RuntimeError("尚未打开可用的公众号正文")
        return self._request("save_image", page=self._article_page, image_index=image_index)

    def close(self) -> None:
        """不关闭用户浏览器；此对象不持有外部连接。"""
        self._started = False
        self._article_page = {}
