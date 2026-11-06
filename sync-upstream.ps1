# 从上游取一次更新（上游已停止维护，仅在它意外复活时用）
# 只做 fetch + 预演 merge，不自动提交、不自动 push。
$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$base = 'baseline-v2.0.0'
git fetch upstream --tags --prune
$new = git log --oneline "$base..upstream/main"
if (-not $new) {
    Write-Host "上游无新提交（相对 $base），无需同步。"
    exit 0
}

Write-Host "上游新增提交：" -ForegroundColor Yellow
$new

$dirty = git status --porcelain
if ($dirty) {
    Write-Host "工作区有未提交改动，先 commit/stash 再同步。" -ForegroundColor Red
    exit 1
}

# 预演：不真的 merge，只看会不会冲突
git merge --no-commit --no-ff upstream/main
if ($LASTEXITCODE -ne 0) {
    Write-Host "有冲突，已停下。解决后 git commit；放弃用 git merge --abort。" -ForegroundColor Red
    exit 1
}
Write-Host "预演成功（改动已暂存未提交）。检查后执行 git commit 完成同步。" -ForegroundColor Green
