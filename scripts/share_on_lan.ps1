<#
Share the Photo Scanner (running in WSL) with devices on the local network.

Run in an ADMINISTRATOR PowerShell:
    powershell -ExecutionPolicy Bypass -File C:\Users\TSF2\Photo_Scanner_Banana\scripts\share_on_lan.ps1
Stop sharing:
    powershell -ExecutionPolicy Bypass -File C:\Users\TSF2\Photo_Scanner_Banana\scripts\share_on_lan.ps1 -Remove

What it does:
  1. Forwards Windows port <Port> to the WSL VM (netsh portproxy). WSL gets a new IP after a reboot or
     `wsl --shutdown`, so run this script again then.
  2. Adds a firewall rule allowing that port ONLY from the local subnet (not the internet, not other networks).

The app has NO login (docs/diagnostics&bugs.md B-3): anyone on this network can view, edit and export photos.
The server inside WSL must listen on 0.0.0.0:  BANANA_HOST=0.0.0.0 bash scripts/dev_live.sh
#>
param(
    [int]$Port = 8000,
    [string]$Distro = "Ubuntu-24.04",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
$ruleName = "Photo Scanner (LAN, port $Port)"

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error "Run this script from an administrator PowerShell."
}

# LAN addresses of this PC (Wi-Fi/Ethernet). Excludes loopback, link-local, WSL/Hyper-V (172.16/12) and Tailscale (100.x).
$lanIps = @(Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.PrefixOrigin -in 'Dhcp', 'Manual' -and $_.IPAddress -notmatch '^(127\.|169\.254\.|172\.(1[6-9]|2\d|3[01])\.|100\.)' } |
    Select-Object -ExpandProperty IPAddress)

# Remove previous forwarding for this port. Listen on the LAN addresses only: WSL's own localhost forwarding
# already holds 127.0.0.1:$Port on Windows, and a 0.0.0.0 listener would collide with it.
foreach ($existing in (netsh interface portproxy show v4tov4 | Select-String "^\s*(\d+\.\d+\.\d+\.\d+)\s+$Port\s")) {
    $address = $existing.Matches[0].Groups[1].Value
    netsh interface portproxy delete v4tov4 listenaddress=$address listenport=$Port | Out-Null
}
Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule

if ($Remove) {
    Write-Host "Sharing stopped: port $Port forwarding and firewall rule removed."
    exit 0
}
if (-not $lanIps) { Write-Error "No LAN address found (is Wi-Fi or Ethernet connected?)." }

$wslIp = ((wsl -d $Distro -- hostname -I) -split "\s+" | Where-Object { $_ -match '^\d+\.\d+\.\d+\.\d+$' } | Select-Object -First 1)
if (-not $wslIp) { Write-Error "Couldn't get the IP address of WSL distro $Distro. Is it running?" }

foreach ($ip in $lanIps) {
    netsh interface portproxy add v4tov4 listenaddress=$ip listenport=$Port connectaddress=$wslIp connectport=$Port | Out-Null
}
New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
    -RemoteAddress LocalSubnet -Profile Any | Out-Null

Write-Host "Forwarding port $Port on $($lanIps -join ', ') -> WSL $wslIp`:$Port (firewall: local subnet only)."
foreach ($ip in $lanIps) { Write-Host "Open on another device:  http://$ip`:$Port/" }
Write-Host "Stop sharing with:  ...\share_on_lan.ps1 -Remove"
