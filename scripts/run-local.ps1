# Runs on a computer in Israel (Windows Task Scheduler, once a day).
# Downloads prices from the chains that refuse connections from GitHub's servers abroad,
# publishes them to the "local" branch, and asks GitHub to rebuild the combined data.
$root = Split-Path -Parent $PSScriptRoot
$py   = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
$git  = 'C:\Program Files\Git\cmd\git.exe'
$gh   = 'C:\Program Files\GitHub CLI\gh.exe'
$repo = 'emattot-design/supermarket-prices'
$work = Join-Path $env:TEMP 'supermarket-prices-local'
$log  = Join-Path $work 'last-run.log'
New-Item -ItemType Directory -Force $work | Out-Null

& $git -C $root pull -q 2>$null   # pick up fixes to the script itself

$env:MODE = 'local'
$env:FIREBASE_PROJECT_ID = 'supermarket-list-8cd93'
$env:FIREBASE_API_KEY = 'AIzaSyCZExR7bQA6CV0nqrcOb1FLUxxr41cHBbM'   # public web key
$env:OUT_DIR = Join-Path $work 'out'
$env:DUMPS_DIR = Join-Path $work 'dumps'
$env:PYTHONIOENCODING = 'utf-8'
# Task Scheduler starts in C:\Windows\System32, where the scraper package cannot write its log file.
Set-Location $work
cmd /c "`"$py`" `"$root\scripts\build.py`" > `"$log`" 2>&1"
if ($LASTEXITCODE -ne 0) { Add-Content $log "build failed with exit code $LASTEXITCODE"; exit 1 }

Set-Location $env:OUT_DIR
if (Test-Path .git) { Remove-Item -Recurse -Force .git }
& $git init -q -b local
& $git config user.name 'prices-bot'
& $git config user.email 'prices-bot@users.noreply.github.com'
& $git add -A
& $git commit -q -m "Local prices $(Get-Date -Format yyyy-MM-dd)"
& $git push -q -f "https://github.com/$repo.git" local
if ($LASTEXITCODE -ne 0) { Add-Content $log 'push failed'; exit 1 }
& $gh workflow run prices.yml -R $repo
Add-Content $log "published $(Get-Date -Format s)"
