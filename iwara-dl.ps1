param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

# ---------- 可调参数 ----------
$MinSizeMB = 100          # 小于该体积(MB)的视频不下载；0 = 不过滤（运行时可用 min 命令改）
$PreviewRetainHours = 2   # 视频下载完成后，预览小片再保留几小时就删（0 = 立刻删）

# ---------- 工具链 ----------
$ytdlpDir = Join-Path $env:APPDATA 'Python\Python314\Scripts'
$wingetLinks = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Links'
$env:PATH = "$ytdlpDir;$wingetLinks;$env:PATH"
$env:PYTHONIOENCODING = 'utf-8'
$ffmpeg = Join-Path $wingetLinks 'ffmpeg.exe'
$ytdlp = Join-Path $ytdlpDir 'yt-dlp.exe'

if (-not (Test-Path $ytdlp)) {
    Write-Host "[错误] 找不到 yt-dlp：$ytdlp" -ForegroundColor Red
    Write-Host "请先运行：python -m pip install --user --upgrade yt-dlp" -ForegroundColor Yellow
    Read-Host "按回车关闭"; exit 1
}

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
$topPy = Join-Path $PSScriptRoot 'iwara_top.py'
$sizePy = Join-Path $PSScriptRoot 'iwara_size.py'

$out = Join-Path $PSScriptRoot 'downloads'
New-Item -ItemType Directory -Force -Path $out | Out-Null
$archive = Join-Path $out 'archive.txt'
$lastList = Join-Path $out '_last_list.html'
$previewDir = Join-Path $out 'previews'
$pendingFile = Join-Path $previewDir '.pending_delete.json'
$template = Join-Path $out '%(uploader)s - %(title).80s [%(id)s].%(ext)s'

$base = @(
    '--impersonate', 'chrome'
    '--windows-filenames'
    '--no-mtime'
    '--continue'
    '--concurrent-fragments', '4'
    '--retries', '10'
    '--download-archive', $archive
    '-o', $template
    '--ffmpeg-location', $ffmpeg
)
$accountFile = Join-Path $PSScriptRoot 'account.txt'
if (Test-Path $accountFile) { $base += @('--config-locations', $accountFile) }

# ---------- 工具函数 ----------
function Get-UrlsFromFile {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return @() }
    Get-Content -LiteralPath $Path -Encoding UTF8 |
        ForEach-Object {
            $line = $_.Trim()
            if (-not $line) { return }
            # HTML 的 href / 纯文本 URL 都吃；停在引号、尖括号、空白处
            $m = [regex]::Matches($line, "https?://[^\s`"<>|]+")
            if ($m.Count -gt 0) { $m[$m.Count - 1].Value.TrimEnd('）', '，', '。', '】', ')', ',', '.') }
        } |
        Where-Object { $_ -match '^https://www\.iwara\.tv/video/' } |
        Select-Object -Unique
}

function Open-List {
    param([string]$Path)
    if (-not (Test-Path $Path)) { return }
    try { Start-Process $Path } catch { Write-Host "  打不开浏览器: $($_.Exception.Message)" -ForegroundColor DarkYellow }
}

# ---------- 预览小片：下载后延时清理 ----------
function Read-PendingDelete {
    if (-not (Test-Path $pendingFile)) { return @{} }
    try {
        $raw = Get-Content -LiteralPath $pendingFile -Raw -Encoding UTF8
        if ([string]::IsNullOrWhiteSpace($raw)) { return @{} }
        $obj = $raw | ConvertFrom-Json
        $h = @{}
        foreach ($p in $obj.PSObject.Properties) { $h[$p.Name] = [long]$p.Value }
        return $h
    } catch { return @{} }
}

function Save-PendingDelete {
    param([hashtable]$Map)
    if (-not (Test-Path $previewDir)) { New-Item -ItemType Directory -Force -Path $previewDir | Out-Null }
    if (-not $Map -or $Map.Count -eq 0) {
        if (Test-Path $pendingFile) { Remove-Item $pendingFile -Force -ErrorAction SilentlyContinue }
        return
    }
    ($Map | ConvertTo-Json -Compress) | Set-Content -LiteralPath $pendingFile -Encoding UTF8
}

function Register-PreviewExpiry {
    param([string]$Id)
    if ([string]::IsNullOrWhiteSpace($Id)) { return }
    if (-not (Test-Path (Join-Path $previewDir "$Id.mp4"))) { return }   # 没预览片就不用登记
    $map = Read-PendingDelete
    if (-not $map.ContainsKey($Id)) {
        $map[$Id] = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
        Save-PendingDelete -Map $map
    }
}

function Invoke-PreviewCleanup {
    param([switch]$Quiet)
    if (-not (Test-Path $previewDir)) { return }
    $map = Read-PendingDelete
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $limit = [long]($PreviewRetainHours * 3600)
    $removed = 0; $bytes = 0

    foreach ($id in @($map.Keys)) {
        if (($now - [long]$map[$id]) -ge $limit) {
            $clip = Join-Path $previewDir "$id.mp4"
            if (Test-Path $clip) {
                $bytes += (Get-Item $clip).Length
                Remove-Item $clip -Force -ErrorAction SilentlyContinue
                $removed++
            }
            $map.Remove($id)
        }
    }

    # 兜底：已下载（在 archive 里）但没登记的旧片，按文件修改时间判断
    if (Test-Path $archive) {
        try {
            $arch = Get-Content -LiteralPath $archive -Raw -Encoding UTF8
            if ($arch) {
                Get-ChildItem $previewDir -Filter '*.mp4' -File -ErrorAction SilentlyContinue | ForEach-Object {
                    if ($arch -match [regex]::Escape($_.BaseName)) {
                        $age = $now - ([DateTimeOffset]$_.LastWriteTimeUtc).ToUnixTimeSeconds()
                        if ($age -ge $limit) {
                            $bytes += $_.Length
                            Remove-Item $_.FullName -Force -ErrorAction SilentlyContinue
                            $removed++
                        }
                    }
                }
            }
        } catch { }
    }

    Save-PendingDelete -Map $map
    if ($removed -gt 0 -and -not $Quiet) {
        Write-Host ("  已清理预览小片 {0} 个（{1} KB）" -f $removed, [math]::Round($bytes / 1KB, 0)) -ForegroundColor DarkGray
    }
}

function Test-SizeGate {
    param([string]$Url, [string]$Format = '')
    $script:sizeInfo = ''
    if ($MinSizeMB -le 0) { return $true }
    if (-not $python -or -not (Test-Path $sizePy)) { return $true }

    $sizeArgs = @($sizePy, $Url, '--min', "$($MinSizeMB)M")
    if ($Format) { $sizeArgs += @('--format', $Format) }
    $lines = & $python @sizeArgs 2>&1
    $rc = $LASTEXITCODE
    $msg = (@($lines | ForEach-Object { "$_".Trim() } | Where-Object { $_ }) -join ' ')
    $script:sizeInfo = $msg
    if ($rc -eq 3) { return $false }
    if ($rc -eq 0) { return $true }
    $script:sizeInfo = '体积探测失败 -> 放行'
    return $true
}

function Resolve-IndexSet {
    param([string]$Spec, [int]$Max)
    $set = New-Object 'System.Collections.Generic.HashSet[int]'
    foreach ($part in ($Spec -split '[,，\s]+' | Where-Object { $_ })) {
        if ($part -match '^(\d+)-(\d+)$') {
            $a = [int]$Matches[1]; $b = [int]$Matches[2]
            if ($a -gt $b) { $t = $a; $a = $b; $b = $t }
            for ($k = $a; $k -le $b; $k++) { if ($k -ge 1) { [void]$set.Add($k) } }
        } elseif ($part -match '^\d+$') {
            [void]$set.Add([int]$part)
        } else {
            Write-Host "  无法识别的序号片段: $part（示例: 2,5,7-9）" -ForegroundColor DarkYellow
        }
    }
    foreach ($k in @($set)) { if ($k -gt $Max) { Write-Host "  序号 $k 超出范围（共 $Max 条），已忽略" -ForegroundColor DarkYellow; [void]$set.Remove($k) } }
    return , $set
}

function Invoke-Downloads {
    param([string[]]$Urls, [string[]]$Extra = @(), $Skip = $null, $Only = $null)
    if (-not $Urls -or $Urls.Count -eq 0) {
        Write-Host '  没有可下载的链接。' -ForegroundColor Yellow
        return
    }
    $total = $Urls.Count

    # 按序号过滤：--skip 排除 / --only 仅下
    $queue = @()
    for ($j = 1; $j -le $total; $j++) {
        if ($Skip -and $Skip.Contains($j)) { continue }
        if ($Only -and $Only.Count -gt 0 -and -not $Only.Contains($j)) { continue }
        $queue += [pscustomobject]@{ Idx = $j; Url = $Urls[$j - 1] }
    }
    $manual = $total - $queue.Count
    if ($manual -gt 0) {
        Write-Host ("  清单共 {0} 条，按规则跳过 {1} 条，实际处理 {2} 条" -f $total, $manual, $queue.Count) -ForegroundColor DarkYellow
    }
    if ($queue.Count -eq 0) {
        Write-Host '  过滤后没有剩余项，什么都没做。' -ForegroundColor Yellow
        return
    }

    $ok = 0; $fail = @(); $skip = 0
    # 若用户用 -f/--format 指定了画质，体积检查按同一画质判断
    $fmtArg = ''
    for ($k = 0; $k -lt $Extra.Count; $k++) {
        if ($Extra[$k] -in @('-f', '--format') -and ($k + 1) -lt $Extra.Count) { $fmtArg = $Extra[$k + 1] }
    }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    for ($i = 0; $i -lt $queue.Count; $i++) {
        $item = $queue[$i]
        Write-Host ''
        Write-Host ("[{0}/{1}] #{2} {3}" -f ($i + 1), $queue.Count, $item.Idx, $item.Url) -ForegroundColor Cyan

        if (-not (Test-SizeGate -Url $item.Url -Format $fmtArg)) {
            Write-Host ("  跳过：{0}" -f $script:sizeInfo) -ForegroundColor DarkYellow
            $skip++
            continue
        }
        if ($script:sizeInfo) { Write-Host ("  体积 {0}" -f $script:sizeInfo) -ForegroundColor DarkGray }

        & $ytdlp @base @Extra $item.Url
        if ($LASTEXITCODE -eq 0) {
            $ok++
            $vid = ''
            if ($item.Url -match '/video/([A-Za-z0-9]+)') { $vid = $Matches[1] }
            Register-PreviewExpiry -Id $vid
        } else { $fail += "#$($item.Idx) $($item.Url)" }
    }
    $sw.Stop()
    Write-Host ''
    Write-Host '  ===== 批量结束 =====' -ForegroundColor DarkGray
    Write-Host ("  成功 {0} ｜ 体积跳过 {1}（<{2}MB）｜ 手动跳过 {3} ｜ 失败 {4} ｜ 清单 {5} 条，耗时 {6:mm\:ss}" -f `
            $ok, $skip, $MinSizeMB, $manual, $fail.Count, $total, $sw.Elapsed) -ForegroundColor Green
    if ($fail.Count -gt 0) {
        Write-Host '  失败列表（可直接重跑，已下载的会被 archive 跳过）:' -ForegroundColor Yellow
        $fail | ForEach-Object { Write-Host "    $_" -ForegroundColor DarkYellow }
    }
    Write-Host ("  输出目录: {0}" -f $out) -ForegroundColor DarkGray
    if ($ok -gt 0) {
        Write-Host ("  预览小片将在 {0} 小时后随清理删除（preview-keep 可改，preview-clean 立即清）" -f $PreviewRetainHours) -ForegroundColor DarkGray
    }
    Invoke-PreviewCleanup
}

function Invoke-List {
    param([string]$Rest)
    if (-not $python) { Write-Host '  找不到 python，无法执行 list。' -ForegroundColor Red; return }
    if (-not (Test-Path $topPy)) { Write-Host "  找不到 $topPy" -ForegroundColor Red; return }

    # 三个位置参数: 页数(或范围) / 爱心下限 / 显示条数；其余选项透传
    $boolFlags = @('--preview', '--urls', '--links', '--demo', '--keep-order', '--no-proxy')
    $tok = @($Rest -split '\s+' | Where-Object { $_ -ne '' })
    $pyArgs = @(); $pos = 0; $i = 0
    while ($i -lt $tok.Count) {
        $t = $tok[$i]
        if ($t -like '-*') {
            $pyArgs += $t
            if (($boolFlags -notcontains $t) -and ($i + 1 -lt $tok.Count) -and $tok[$i + 1] -notlike '-*') {
                $pyArgs += $tok[$i + 1]; $i += 2; continue
            }
            $i++; continue
        }
        switch ($pos) {
            0 { $pyArgs += @('--pages', $t) }
            1 { $pyArgs += @('--min-likes', $t) }
            2 { $pyArgs += @('--count', $t) }
            default { $pyArgs += $t }
        }
        $pos++; $i++
    }
    if ($pos -lt 2) { $pyArgs = @('--pages', '3', '--min-likes', '0') + $pyArgs }

    & $python $topPy @pyArgs --save-report $lastList
    Write-Host ''
    Write-Host ("  清单已写入: {0}" -f $lastList) -ForegroundColor DarkGray
    if (-not $env:IWARA_NO_OPEN) { Open-List $lastList }
    Write-Host '  下一步: dl 全下 ｜ dl --skip 2,5 跳过某几项 ｜ dl --only 1,4 只下某几项 ｜ open 重新打开清单' -ForegroundColor Gray
}

function Show-Help {
    Write-Host ''
    Write-Host '  可用命令' -ForegroundColor Cyan
    Write-Host '  ------------------------------------------------------------' -ForegroundColor DarkGray
    Write-Host '   <链接>                       ' -NoNewline -ForegroundColor White
    Write-Host '下载视频/作者页（可空格分隔多个）'
    Write-Host '   list <页> <爱心> [条数]      ' -NoNewline -ForegroundColor White
    Write-Host '列出命中的视频（表格）'
    Write-Host '   list 3 100                    ' -NoNewline -ForegroundColor White
    Write-Host '最近 3 页，爱心 >= 100（结果存成可点击的 HTML）'
    Write-Host '   list 2-5 100 10               ' -NoNewline -ForegroundColor White
    Write-Host '第 2~5 页，爱心 >= 100，只显示前 10 条'
    Write-Host '   list 2-5 100 --user NAME      ' -NoNewline -ForegroundColor White
    Write-Host '只查某个作者'
    Write-Host '   list 3 100 --sort likes       ' -NoNewline -ForegroundColor White
    Write-Host '排序: date/trending/popular/views/likes'
    Write-Host '   list 3 100 --urls             ' -NoNewline -ForegroundColor White
    Write-Host '输出链接而不是表格'
    Write-Host '   list 3 100 --links            ' -NoNewline -ForegroundColor White
    Write-Host '终端表格里也显示完整链接列'
    Write-Host '   list 3 100 --preview          ' -NoNewline -ForegroundColor White
    Write-Host '生成 6 秒悬停预览（整片均匀取 6 段，首次较慢）'
    Write-Host '   list 3 100 --preview --preview-segments 8 --preview-seconds 8' -ForegroundColor DarkGray
    Write-Host '   list 3 100 --preview --preview-width 640' -ForegroundColor DarkGray
    Write-Host '   list --demo                   ' -NoNewline -ForegroundColor White
    Write-Host '用假数据看表格排版'
    Write-Host '   dl [txt路径]                  ' -NoNewline -ForegroundColor White
    Write-Host '批量下载；省略路径 = 上次 list 的结果'
    Write-Host '   dl --skip 2,5,8-10            ' -NoNewline -ForegroundColor White
    Write-Host '跳过清单里这几项（按表格里的序号）'
    Write-Host '   dl --only 1,4                 ' -NoNewline -ForegroundColor White
    Write-Host '只下这几项'
    Write-Host '   dl --skip 3 --only 1-6        ' -NoNewline -ForegroundColor White
    Write-Host '可组合，两个规则叠加'
    Write-Host '   min <MB>                      ' -NoNewline -ForegroundColor White
    Write-Host '改体积下限（当前见下行），0 = 不过滤'
    Write-Host '   preview-keep <小时>           ' -NoNewline -ForegroundColor White
    Write-Host '改「下载后预览小片保留几小时」，0 = 立即删'
    Write-Host '   preview-clean                 ' -NoNewline -ForegroundColor White
    Write-Host '立刻执行一次过期清理，并列出剩余数量'
    Write-Host '   help / q                      ' -NoNewline -ForegroundColor White
    Write-Host '帮助 / 退出（直接回车也退出）'
    Write-Host '   open                          ' -NoNewline -ForegroundColor White
    Write-Host '在浏览器里重新打开上次的清单'
    Write-Host ''
    Write-Host '  注: 爱心下限是 >=（含等于）；页码从 1 开始。' -ForegroundColor DarkGray
}

# ---------- 启动时先做一次过期清理 ----------
Invoke-PreviewCleanup -Quiet

# ---------- 带参数：命令行 / 拖拽模式 ----------
if ($Args.Count -gt 0) {
    $opts = @(); $targets = @()
    for ($i = 0; $i -lt $Args.Count; $i++) {
        $a = $Args[$i]
        if ($a -like '-*') {
            $opts += $a
            if ($i + 1 -lt $Args.Count) {
                $n = $Args[$i + 1]
                if ($n -notlike '-*' -and $n -notmatch '^https?://' -and -not (Test-Path $n)) {
                    $opts += $n; $i++
                }
            }
        } else { $targets += $a }
    }

    $urls = @()
    foreach ($t in $targets) {
        if ((Test-Path $t -PathType Leaf) -and $t -match '\.txt$') { $urls += Get-UrlsFromFile $t }
        elseif ($t -match '^https?://') { $urls += $t }
    }

    if ($urls.Count -eq 0 -and $opts.Count -gt 0) {
        & $ytdlp @base @opts
        exit $LASTEXITCODE
    }
    if ($urls.Count -eq 0) {
        Write-Host '没有识别到链接或 txt 文件。' -ForegroundColor Yellow
        exit 1
    }
    Invoke-Downloads -Urls $urls -Extra $opts
    exit 0
}

# ---------- 无参数：双击进入交互模式 ----------
$host.UI.RawUI.WindowTitle = 'iwara 下载器'
Write-Host ''
Write-Host '  iwara 下载器' -ForegroundColor Cyan
Write-Host '  ------------------------------------------------------------' -ForegroundColor DarkGray
Write-Host '  输出目录: ' -NoNewline -ForegroundColor Gray
Write-Host $out -ForegroundColor Yellow
Write-Host ("  体积下限: {0}" -f $(if ($MinSizeMB -gt 0) { ">= ${MinSizeMB} MB（更小的跳过）" } else { '不过滤' })) -ForegroundColor Yellow
Write-Host ("  预览保留: 下载完成后 {0} 小时自动删除" -f $PreviewRetainHours) -ForegroundColor Yellow
Write-Host '  直接回车退出；输入 help 看全部命令。' -ForegroundColor DarkGray
if (Test-Path $accountFile) { Write-Host '  已检测到 account.txt。' -ForegroundColor Green }
Write-Host ''

while ($true) {
    $line = Read-Host '命令/链接'
    if ([string]::IsNullOrWhiteSpace($line)) { break }
    $line = $line.Trim()
    if ($line -match '^(q|quit|exit)$') { break }
    if ($line -match '^(help|h|\?)$') { Show-Help; continue }

    if ($line -match '^min\s+(\d+)$') {
        $MinSizeMB = [int]$Matches[1]
        Write-Host ("  体积下限已设为 {0}" -f $(if ($MinSizeMB -gt 0) { "$MinSizeMB MB" } else { '不过滤' })) -ForegroundColor Green
        continue
    }

    if ($line -match '^preview-keep\s+(\d+)$') {
        $PreviewRetainHours = [int]$Matches[1]
        Write-Host ("  下载后预览小片保留时间已设为 {0} 小时{1}" -f $PreviewRetainHours,
            $(if ($PreviewRetainHours -eq 0) { '（下次清理即删）' } else { '' })) -ForegroundColor Green
        Invoke-PreviewCleanup
        continue
    }

    if ($line -match '^preview-clean$') {
        Invoke-PreviewCleanup
        $map = Read-PendingDelete
        $cnt = 0
        if (Test-Path $previewDir) { $cnt = (Get-ChildItem $previewDir -Filter '*.mp4' -File | Measure-Object).Count }
        Write-Host ("  当前预览小片 {0} 个，待删登记 {1} 条" -f $cnt, $map.Count) -ForegroundColor Gray
        continue
    }

    if ($line -match '^(list|ls)\b(.*)$') { Invoke-List -Rest $Matches[2].Trim(); continue }

    if ($line -match '^(open|show)$') { Open-List $lastList; continue }

    if ($line -match '^(dl|download|batch)\b(.*)$') {
        $rest = $Matches[2].Trim()
        $tok = @($rest -split '\s+' | Where-Object { $_ })
        $p = ''; $skipSpec = ''; $onlySpec = ''
        for ($k = 0; $k -lt $tok.Count; $k++) {
            $t = $tok[$k]
            if ($t -match '^--?(skip|x)=(.*)$') { $skipSpec = $Matches[2]; continue }
            if ($t -match '^--?only=(.*)$') { $onlySpec = $Matches[1]; continue }
            if ($t -in @('--skip', '-x', '-skip')) {
                if ($k + 1 -lt $tok.Count) { $skipSpec = $tok[$k + 1]; $k++ } else { Write-Host '  --skip 后面要跟序号，如 --skip 2,5' -ForegroundColor DarkYellow }
                continue
            }
            if ($t -in @('--only', '-only')) {
                if ($k + 1 -lt $tok.Count) { $onlySpec = $tok[$k + 1]; $k++ } else { Write-Host '  --only 后面要跟序号，如 --only 1,4' -ForegroundColor DarkYellow }
                continue
            }
            if (-not $p) { $p = $t }
        }
        $p = $p.Trim('"').Trim("'")
        if ([string]::IsNullOrWhiteSpace($p)) { $p = $lastList }
        if (-not (Test-Path $p)) {
            Write-Host "  找不到清单文件: $p" -ForegroundColor Red
            Write-Host '  先跑 list 生成，或拖一个 txt 进来。' -ForegroundColor DarkGray
            continue
        }
        $u = Get-UrlsFromFile $p
        Write-Host ("  从 {0} 读到 {1} 条链接" -f (Split-Path $p -Leaf), $u.Count) -ForegroundColor Gray
        $skipSet = if ($skipSpec) { Resolve-IndexSet -Spec $skipSpec -Max $u.Count } else { $null }
        $onlySet = if ($onlySpec) { Resolve-IndexSet -Spec $onlySpec -Max $u.Count } else { $null }
        if ($skipSet -and $onlySet) { Write-Host '  同时给了 --skip 和 --only，两个规则会叠加生效。' -ForegroundColor DarkYellow }
        Invoke-Downloads -Urls $u -Skip $skipSet -Only $onlySet
        continue
    }

    # 其余按链接处理：支持一次粘多个
    $urls = @($line.Trim('"') -split '\s+' | Where-Object { $_ -match '^https?://' })
    if ($urls.Count -eq 0) {
        Write-Host '  无法识别：既不是命令也不是 http(s) 链接。输入 help 查看用法。' -ForegroundColor Red
        continue
    }
    Invoke-Downloads -Urls $urls
}

Write-Host '  已退出。' -ForegroundColor Gray
Start-Sleep -Milliseconds 400
