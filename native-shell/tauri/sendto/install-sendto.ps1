# install-sendto.ps1 — 在 Windows「发送到」菜单里注册「收藏箱」入口。
# 用法：右键此脚本 →「使用 PowerShell 运行」（或以管理员运行，若 SendTo 目录只读）。
$dir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $dir "SendTo-收藏箱.ps1"
$sendTo = [Environment]::GetFolderPath('SendTo')
$lnkPath = Join-Path $sendTo "发送到收藏箱.lnk"

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnkPath)
$sc.TargetPath      = "powershell.exe"
# SendTo 会自动把「你右键的那个文件路径」追加为最后一个参数 → 绑定 $Target
$sc.Arguments       = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$script`""
$sc.IconLocation    = "shell32.dll,23"
$sc.Description     = "把链接 / 文本收进收藏箱"
$sc.Save()

Write-Host "已注册：【发送到 → 收藏箱】" -ForegroundColor Green
Write-Host "位置：$lnkPath"
Write-Host "现在复制一个 GitHub/GitLab 链接，右键 → 发送到 → 收藏箱 即可体验。"