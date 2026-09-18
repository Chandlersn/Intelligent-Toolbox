# SendTo-收藏箱.ps1 — Windows「发送到」菜单入口。
# 右键任意文件/空白 →「发送到」→「收藏箱」，把当前剪贴板里的链接/文本送给收集器收藏。
# 与移动端系统分享（ShareReceiver）形成闭环：桌面单次右键即收藏，无需打开网页粘贴。
param([string]$Target = "")

$collector = "http://127.0.0.1:8732/collect"
$text = ""

# 优先用右键选中的项：若它本身就是一个完整 URL（如收藏的链接），直接用；
# 否则退回读剪贴板（桌面「分享」最通用的语义 = 复制过的东西）。
if (-not [string]::IsNullOrWhiteSpace($Target) -and $Target -match '^https?://') {
    $text = $Target
} else {
    try { $text = Get-Clipboard -Raw -ErrorAction Stop } catch { }
}

if ([string]::IsNullOrWhiteSpace($text)) {
    Write-Host "剪贴板为空，请先复制要收藏的链接或文本。" -ForegroundColor Yellow
    Start-Sleep -Milliseconds 1500
    return
}

try {
    $body = @{ url = ""; text = $text } | ConvertTo-Json -Compress
    $resp = Invoke-RestMethod -Uri $collector -Method Post -ContentType "application/json" -Body $body -TimeoutSec 15
    if ($resp.ok) {
        $tip = if ($resp.dup) { "已在收藏箱中 ✓" } else { "已收藏 ✓" }
        Write-Host $tip -ForegroundColor Green
    } else {
        Write-Host "收藏失败：$($resp.error)" -ForegroundColor Yellow
    }
} catch {
    Write-Host "收集器未运行？请先启动后端（python run.py）再重试。" -ForegroundColor Yellow
    Write-Host "错误：$($_.Exception.Message)"
}
Start-Sleep -Milliseconds 1500