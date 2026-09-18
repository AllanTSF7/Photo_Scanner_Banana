<#
Expose the Photo Scanner (running in WSL, listening on 127.0.0.1:<Port>) on this device's own tailnet address -
e.g. http://100.92.43.68:8000/ - so it's reachable from a phone/iPad/laptop on the same tailnet without any
LAN port-forwarding (see share_on_lan.ps1 for that separate, LAN-only path).

Run in an ordinary PowerShell (no admin needed):
    powershell -ExecutionPolicy Bypass -File C:\Users\TSF2\Photo_Scanner_Banana\scripts\serve_on_tailnet.ps1
Stop sharing:
    powershell -ExecutionPolicy Bypass -File C:\Users\TSF2\Photo_Scanner_Banana\scripts\serve_on_tailnet.ps1 -Remove

Safe to run every time the app starts: if Tailscale isn't installed or this device isn't connected, it prints
that and exits cleanly (0) rather than failing the startup - "start on the tailnet if it's available".

Uses raw TCP forwarding (`tailscale serve --tcp`), not HTTP mode: the app is plain HTTP, and TCP mode avoids
Tailscale terminating TLS and needing a cert for a plain-HTTP backend. WSL2's own localhost forwarding already
makes 127.0.0.1:<Port> on Windows reach the app inside WSL, so this only has to reach that.
#>
param(
    [int]$Port = 8000,
    [switch]$Remove
)

$tailscale = Get-Command tailscale -ErrorAction SilentlyContinue
if (-not $tailscale) {
    Write-Host "Tailscale isn't installed - skipping tailnet sharing."
    exit 0
}

$selfIp = (& tailscale ip -4 2>$null)
if (-not $selfIp) {
    Write-Host "Tailscale isn't connected on this device - skipping tailnet sharing."
    exit 0
}

if ($Remove) {
    & tailscale serve --tcp=$Port off 2>&1 | Out-Null
    Write-Host "Tailnet sharing stopped for port $Port."
    exit 0
}

& tailscale serve --bg --tcp=$Port "tcp://localhost:$Port" 2>&1 | Out-Null

$dnsName = ((& tailscale status --json | ConvertFrom-Json).Self.DNSName).TrimEnd(".")
Write-Host "On the tailnet:  http://$selfIp`:$Port/"
if ($dnsName) { Write-Host "             or  http://$dnsName`:$Port/" }
Write-Host "Stop with:  ...\serve_on_tailnet.ps1 -Remove"
