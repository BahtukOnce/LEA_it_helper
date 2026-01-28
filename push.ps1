git add .

$msg = Read-Host "Commit message (Enter = auto)"

if ([string]::IsNullOrWhiteSpace($msg)) {
    $msg = "update " + (Get-Date -Format "yyyy-MM-dd HH:mm")
}

git commit -m "$msg"
git push

Get-Content "bot.py" | Set-Content "C:\Users\ea.liazin\Desktop\bot.txt"

Write-Output "Done!"