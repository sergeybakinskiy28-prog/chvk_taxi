param($msg = "auto-update")

$SSH_HOST = "root@sergeybakinskiy281"
$REMOTE_DIR = "~/chvk_taxi"

# 1. Local: commit and push
Write-Host ""
Write-Host "[1/3] Git: add -> commit -> push..." -ForegroundColor Cyan

git add .

$changes = git status --porcelain
if ($changes) {
    git commit -m $msg
} else {
    Write-Host "      No changes to commit, skipping." -ForegroundColor Yellow
}

git push origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: git push failed." -ForegroundColor Red
    exit 1
}
Write-Host "      Push OK." -ForegroundColor Green

# 2. Remote: pull + rebuild
Write-Host ""
Write-Host "[2/3] SSH -> git pull + docker compose up --build..." -ForegroundColor Cyan

ssh $SSH_HOST "cd $REMOTE_DIR && git pull origin main && docker compose up -d --build"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: SSH command failed." -ForegroundColor Red
    exit 1
}

# 3. Done
Write-Host ""
Write-Host "[3/3] Deploy complete! Bot restarted on server." -ForegroundColor Green
Write-Host ""
