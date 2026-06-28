[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# 从订阅测试所有节点延迟
$sub = 'https://s-dywrwizazu.cn-shanghai.fcapp.run/okz/sub?token=7c00e506cb827821ad76e93053737c61'
$resp = Invoke-WebRequest $sub -TimeoutSec 15 -UseBasicParsing
$raw = [Convert]::FromBase64String($resp.Content)
$decoded = [Text.Encoding]::UTF8.GetString($raw)
$urls = $decoded.Split("`n") | Where-Object { $_.Trim() -ne '' }

$results = @()
foreach ($url in $urls) {
    if ($url -notmatch '^(\w+)://.+@(.+):(\d+)') { continue }
    $type = $Matches[1]
    $host_ = $Matches[2]
    $port = [int]$Matches[3]

    try {
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $tcp = New-Object System.Net.Sockets.TcpClient
        $task = $tcp.ConnectAsync($host_, $port)
        if ($task.Wait(5000)) {
            $sw.Stop()
            if ($tcp.Connected) {
                $results += [PSCustomObject]@{Status='OK'; Latency=$sw.ElapsedMilliseconds; Host=$host_; Port=$port; Type=$type; Error=''}
            } else {
                $results += [PSCustomObject]@{Status='FAIL'; Latency=99999; Host=$host_; Port=$port; Type=$type; Error='not connected'}
            }
        } else {
            $sw.Stop()
            $results += [PSCustomObject]@{Status='FAIL'; Latency=$sw.ElapsedMilliseconds; Host=$host_; Port=$port; Type=$type; Error='timeout'}
        }
        $tcp.Close()
    } catch {
        $results += [PSCustomObject]@{Status='FAIL'; Latency=99999; Host=$host_; Port=$port; Type=$type; Error=$_.Exception.Message.Substring(0,[Math]::Min(40,$_.Exception.Message.Length))}
    }
}

$ok = @($results | Where-Object { $_.Status -eq 'OK' })
$fail = @($results | Where-Object { $_.Status -eq 'FAIL' })
Write-Output "=== $($ok.Count) OK / $($fail.Count) FAIL ==="
$ok | Sort-Object Latency | Select-Object Latency, Host, Port, Type | Format-Table -AutoSize
Write-Output "--- FAIL (first 10) ---"
$fail | Select-Object Host, Port, Type, Error | Select-Object -First 10 | Format-Table -AutoSize

# 保存可用节点列表到 WSL 路径
if ($ok.Count -gt 0) {
    $outPath = "\\wsl.localhost\Ubuntu\home\administrator\dkk-projects\auto-switch-ip\win_probe_results.json"
    $ok | Sort-Object Latency | ConvertTo-Json -Depth 3 | Out-File $outPath -Encoding UTF8
    Write-Output "Saved to $outPath"
}
