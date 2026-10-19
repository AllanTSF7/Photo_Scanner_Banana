<#
Prepare a Windows machine to receive Photo Scanner transfers (relay/compute peer). Run it ON THE PEER, in an
ADMINISTRATOR PowerShell:

    powershell -ExecutionPolicy Bypass -File setup_windows_peer.ps1 -PublicKey "ssh-ed25519 AAAA... photoscanner@laptop"

What it does (safe to run again; it changes only what it created):
  1. Makes sure Windows' OpenSSH Server is installed, running, and starts automatically.
  2. Creates a local, non-admin account (default photoscanner-relay) with a long random password that is never
     shown or stored: the account can only sign in with the laptop's key.
  3. Creates the landing folder (default C:\PhotoScannerRelay with incoming\ and results\), writable by that account.
  4. Installs the laptop's public key for that account only, outside any user profile.
  5. Adds an sshd rule for that account: key only, SFTP/scp only (no shell, no port forwarding), locked inside the
     landing folder. sshd_config is backed up first; if the service does not come back running and answering on
     port 22, the backup is restored and the service restarted.

It does NOT change the firewall rule Windows created for SSH (other uses of SSH on this machine keep working),
and it does not touch any other account or key.

Undo: -Remove (removes the sshd rule, the key file and the account; the landing folder is kept).
Preview/test: -ConfigOnly -ConfigFile <copy of sshd_config> edits only that file (no admin, no service, no account).
#>
param(
    [string]$PublicKey = "",
    [string]$User = "photoscanner-relay",
    [string]$Root = "C:\PhotoScannerRelay",
    [string]$ConfigFile = (Join-Path $env:ProgramData "ssh\sshd_config"),
    [switch]$Remove,
    [switch]$ConfigOnly
)

$ErrorActionPreference = "Stop"

# ---------------------------------------------------------------- sshd_config text (no side effects)
function Get-BeginMark([string]$user) { "# >>> Photo Scanner peer: $user" }
function Get-EndMark([string]$user) { "# <<< Photo Scanner peer: $user" }

function Get-Newline([string]$text) { if ($text -match "`r`n") { "`r`n" } else { "`n" } }

function Remove-PeerBlock([string]$text, [string]$user) {
    # Our block plus the blank line Add-PeerBlock put before it; everything else is left byte for byte.
    $nl = Get-Newline $text
    $pattern = "(?s)(\r?\n){0,2}" + [regex]::Escape((Get-BeginMark $user)) + ".*?" + [regex]::Escape((Get-EndMark $user)) + "(\r?\n)?"
    $out = [regex]::Replace($text, $pattern, $nl)
    return $out.TrimEnd("`r", "`n") + $nl
}

function Add-PeerBlock([string]$text, [string]$user, [string]$root) {
    # Match blocks must come last in sshd_config, so ours is appended after anything already there.
    $nl = Get-Newline $text
    $base = (Remove-PeerBlock $text $user).TrimEnd("`r", "`n")
    $chroot = $root -replace "\\", "/"
    $block = @(
        (Get-BeginMark $user)
        "Match User $user"
        "    AuthorizedKeysFile __PROGRAMDATA__/ssh/photoscanner_$($user)_authorized_keys"
        "    PasswordAuthentication no"
        "    ChrootDirectory ""$chroot"""
        "    ForceCommand internal-sftp"
        "    AllowTcpForwarding no"
        "    PermitTTY no"
        (Get-EndMark $user)
    ) -join $nl
    return $base + $nl + $nl + $block + $nl
}

function Write-ConfigText([string]$path, [string]$text) {
    # No BOM: sshd reads the file as plain text.
    [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding $false))
}

function Update-ConfigFile([string]$path, [switch]$remove) {
    $text = [IO.File]::ReadAllText($path)
    $new = if ($remove) { Remove-PeerBlock $text $User } else { Add-PeerBlock $text $User $Root }
    $backup = "$path.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"
    Copy-Item $path $backup
    Write-ConfigText $path $new
    return $backup
}

# ---------------------------------------------------------------- machine changes
function Restart-SshdChecked([string]$backup) {
    # Not `sshd -t`: from an admin prompt it can't read the host keys (only SYSTEM can) and always fails with
    # "no hostkeys available". The service runs as SYSTEM, so test with it: restart, then require it running and
    # answering on port 22. Otherwise put the backup back and restart again.
    try { Restart-Service sshd -ErrorAction Stop } catch { }
    $ok = $false
    for ($i = 0; $i -lt 10 -and -not $ok; $i++) {
        Start-Sleep -Seconds 1
        $client = New-Object Net.Sockets.TcpClient
        try { $client.Connect("127.0.0.1", 22); $ok = (Get-Service sshd).Status -eq "Running" } catch { } finally { $client.Close() }
    }
    if (-not $ok) {
        Copy-Item $backup $ConfigFile -Force
        Restart-Service sshd
        throw "sshd did not come back with the new rule; the previous config was restored from $backup and the service restarted"
    }
}

function Install-SshServer {
    $cap = Get-WindowsCapability -Online -Name "OpenSSH.Server*" | Select-Object -First 1
    if ($cap.State -ne "Installed") { Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
    Set-Service sshd -StartupType Automatic
    Start-Service sshd
    Write-Host "OpenSSH Server: running, starts automatically"
}

function New-PeerAccount {
    if (Get-LocalUser -Name $User -ErrorAction SilentlyContinue) { Write-Host "Account exists: $User"; return }
    $bytes = New-Object byte[] 48
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $password = ConvertTo-SecureString ([Convert]::ToBase64String($bytes)) -AsPlainText -Force
    New-LocalUser -Name $User -Password $password -PasswordNeverExpires -UserMayNotChangePassword `
        -Description "Photo Scanner transfers (key-only SFTP)" | Out-Null
    Write-Host "Account created: $User (not an administrator)"
}

function New-PeerFolders {
    # The chroot root itself must not be writable by the account (an sshd requirement): write access goes only to
    # the subfolders.
    foreach ($dir in $Root, (Join-Path $Root "incoming"), (Join-Path $Root "results")) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
    }
    foreach ($sub in "incoming", "results") {
        & icacls (Join-Path $Root $sub) /grant "$($User):(OI)(CI)M" | Out-Null
    }
    Write-Host "Landing folder: $Root (incoming\, results\)"
}

function Install-PeerKey([string]$keyFile) {
    # Readable by sshd (SYSTEM), not writable by the account.
    [IO.File]::WriteAllText($keyFile, $PublicKey + "`n", (New-Object Text.UTF8Encoding $false))
    & icacls $keyFile /inheritance:r /grant "SYSTEM:F" /grant "Administrators:F" | Out-Null
    Write-Host "Key installed: $keyFile"
}

# ---------------------------------------------------------------- main
if ($User -notmatch '^[A-Za-z0-9._-]{1,20}$') { throw "Account name may use letters, digits, '.', '_' and '-' only (max 20)." }
if ($Root -match '"') { throw "The landing folder path must not contain quotes." }
if (-not $Remove -and $PublicKey -notmatch '^ssh-ed25519 [A-Za-z0-9+/]+=* ?[^\r\n]*$') {
    throw "Pass the laptop's public key on one line: -PublicKey ""ssh-ed25519 AAAA... name"""
}
if (-not (Test-Path $ConfigFile)) { throw "sshd_config not found: $ConfigFile" }

if ($ConfigOnly) {
    $backup = Update-ConfigFile $ConfigFile -remove:$Remove
    Write-Host "Config only: $ConfigFile updated (backup: $backup); nothing else changed"
    exit 0
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw "Run this from an administrator PowerShell." }
$KeyFile = Join-Path (Split-Path $ConfigFile) "photoscanner_$($User)_authorized_keys"

if ($Remove) {
    Restart-SshdChecked (Update-ConfigFile $ConfigFile -remove)
    Remove-Item $KeyFile -Force -ErrorAction SilentlyContinue
    if (Get-LocalUser -Name $User -ErrorAction SilentlyContinue) { Remove-LocalUser -Name $User }
    Write-Host "Removed the sshd rule, key and account for $User. Landing folder kept: $Root"
    exit 0
}

Install-SshServer
New-PeerAccount
New-PeerFolders
Install-PeerKey $KeyFile
$backup = Update-ConfigFile $ConfigFile
Restart-SshdChecked $backup
Write-Host "sshd rule added (backup: $backup); service restarted"
Write-Host "`nDone. From the laptop, Photo Scanner connects as $User@$env:COMPUTERNAME and sees only incoming/ and results/."
