# One-shot hosts fix: ensure "127.0.0.1 localhost" is the FIRST entry.
# Background: hosts contained Typora-related lines (127.0.0.1 store.typora.io ...),
# so reverse canonicalization of 127.0.0.1 returned store.typora.io. This polluted
# JDK 13+ WEPoll selector wakeup (loopback socketpair resolved by name) -> selectors
# never wake (Neo4j bolt advertised as store.typora.io:7687). Java 8 legacy selector
# is unaffected. Adding localhost as the first hosts entry restores canonical order.
# ASCII only (PS 5.1 reads BOM-less files as ANSI).
$log = "D:\WindLight\Documents\Code\ProjectA\data\run\hosts_fix.log"
try {
    $p = Join-Path $env:SystemRoot "System32\drivers\etc\hosts"
    Copy-Item $p ($p + ".bak-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
    $lines = [System.IO.File]::ReadAllLines($p)
    $hasLocalhost = $false
    foreach ($l in $lines) { if ($l -match "^\s*127\.0\.0\.1\s+localhost(\s|$)") { $hasLocalhost = $true } }
    if ($hasLocalhost) {
        "HOSTS-ALREADY-OK" | Out-File $log -Encoding utf8
    } else {
        $out = New-Object System.Collections.Generic.List[string]
        $out.Add("127.0.0.1 localhost")
        foreach ($l in $lines) { $out.Add($l) }
        [System.IO.File]::WriteAllLines($p, $out)
        "HOSTS-FIXED: localhost moved to first entry (backup created)" | Out-File $log -Encoding utf8
    }
    ipconfig /flushdns | Out-Null
} catch {
    "HOSTS-FIX-FAIL: $($_.Exception.Message)" | Out-File $log -Encoding utf8
    exit 1
}
