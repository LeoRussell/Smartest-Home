# Allow only this project's HTTP/WebSocket TCP ports on one IPv4 LAN.
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [ValidateRange(1, 65535)][int] $HttpPort = 8080,
    [ValidateRange(1, 65535)][int] $GatewayPort = 8765,
    [string] $InterfaceAlias,
    [switch] $Remove
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $PSScriptRoot 'lan-firewall-plan.psm1') -Force
$projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$statePath = Join-Path $PSScriptRoot '.lan-firewall-state.json'
$hasher = [System.Security.Cryptography.SHA256]::Create()
try { $digest = $hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($projectRoot.ToLowerInvariant())) }
finally { $hasher.Dispose() }
$ruleName = 'SmartestHome-LAN-' + ([BitConverter]::ToString($digest).Replace('-', '').Substring(0, 12)) + '-allow'
$state = $null
if (Test-Path -LiteralPath $statePath) {
    $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($state.Format -ne 'smartest-home-lan-firewall-v1' -or $state.ProjectRoot -ne $projectRoot -or $state.RuleName -ne $ruleName) {
        throw 'The saved LAN firewall state does not belong to this project.'
    }
}

if ($Remove) {
    if (-not $state) { Write-Output 'No saved project LAN firewall rule to remove.'; return }
    $rule = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
    if ($rule) {
        $application = $rule | Get-NetFirewallApplicationFilter
        if ($application.Program -ne $state.PythonPath -or $rule.Action -ne 'Allow' -or $rule.Direction -ne 'Inbound') {
            throw 'The project rule was changed for another application; it will not be removed.'
        }
    }
    if ($PSCmdlet.ShouldProcess($projectRoot, 'Remove only the project LAN firewall rule')) {
        if ($rule) { $rule | Remove-NetFirewallRule }
        Remove-Item -LiteralPath $statePath
        Write-Output 'Project LAN rule removed. All pre-existing firewall rules are unchanged.'
    }
    return
}

if ($state) { throw 'LAN rules already have saved state. Use -Remove before changing or reapplying them.' }
if (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue) { throw 'A project rule exists without its saved state; review it before continuing.' }
if ($HttpPort -eq $GatewayPort) { throw 'HTTP and Gateway TCP ports must be different.' }
$python = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw 'Create the project .venv before setting up LAN access.' }
# Windows venv python.exe is a redirector; firewall filters see the real executable.
$pythonPath = (& $python -c 'import ctypes; b=ctypes.create_unicode_buffer(32768); assert ctypes.windll.kernel32.GetModuleFileNameW(None,b,len(b)); print(b.value)').Trim()
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) { throw 'Could not identify the running Python executable.' }
$adapters = @(Get-NetIPConfiguration | Where-Object {
    $_.IPv4DefaultGateway -and $_.IPv4DefaultGateway.NextHop -ne '0.0.0.0' -and
    (-not $InterfaceAlias -or $_.InterfaceAlias -eq $InterfaceAlias)
})
if ($adapters.Count -ne 1) { throw 'Choose one connected LAN adapter with -InterfaceAlias (for example Ethernet or Wi-Fi).' }
$adapter = $adapters[0]
$addresses = @($adapter.IPv4Address | Where-Object { $_.IPAddress -notlike '169.254.*' })
if ($addresses.Count -ne 1) { throw 'The selected adapter must have one usable IPv4 address.' }
$plan = New-LanFirewallPlan -IPv4Address $addresses[0].IPAddress -PrefixLength $addresses[0].PrefixLength -Ports $HttpPort, $GatewayPort
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$tokenSids = @($identity.User.Value) + @($identity.Groups | ForEach-Object { $_.Value })

foreach ($rule in @(Get-NetFirewallRule -PolicyStore ActiveStore -Enabled True -Direction Inbound -Action Block)) {
    $application = $rule | Get-NetFirewallApplicationFilter
    if ($application.Program -notin @($pythonPath, 'Any')) { continue }
    $security = $rule | Get-NetFirewallSecurityFilter
    if (-not (Test-LanFirewallUserScope -LocalUser $security.LocalUser -TokenSids $tokenSids)) { continue }
    $ports = $rule | Get-NetFirewallPortFilter
    if ($ports.Protocol -notin @('TCP', '6', 'Any', '256')) { continue }
    $affected = $false
    foreach ($port in $ports.LocalPort) {
        if ($port -eq 'Any') { $affected = $true; break }
        $bounds = $port -split '-'
        $first = 0
        $last = 0
        if (-not [int]::TryParse($bounds[0], [ref]$first)) { $affected = $true; break }
        if ($bounds.Count -eq 1) { $last = $first }
        elseif (-not [int]::TryParse($bounds[1], [ref]$last)) { $affected = $true; break }
        if (($HttpPort -ge $first -and $HttpPort -le $last) -or ($GatewayPort -ge $first -and $GatewayPort -le $last)) { $affected = $true; break }
    }
    if ($affected) {
        throw "A blocking rule may affect Python on these TCP ports ($($rule.DisplayName)). Existing rules will not be changed; review that policy before continuing."
    }
}
$plannedState = [PSCustomObject]@{
    Format = 'smartest-home-lan-firewall-v1'; ProjectRoot = $projectRoot; RuleName = $ruleName
    PythonPath = $pythonPath; InterfaceAlias = $adapter.InterfaceAlias; Plan = $plan
}
Write-Output ($plannedState | ConvertTo-Json -Depth 5)
if (-not $PSCmdlet.ShouldProcess("$pythonPath on $($adapter.InterfaceAlias)", "Allow TCP $HttpPort,$GatewayPort from $($plan.RemoteSubnet) to $($plan.LocalAddress) only")) { return }
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run this script from PowerShell as Administrator (or use -WhatIf to preview).' }
# Save the single owned rule name before creating it, so -Remove also works
# after an interrupted setup. No existing firewall rules are altered.
$plannedState | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $statePath -Encoding UTF8
try {
    New-NetFirewallRule -Name $ruleName -DisplayName 'Smartest Home: LAN web interface and Gateway' -Direction Inbound -Action Allow -Enabled True -Protocol TCP -Program $pythonPath -LocalPort $plan.Ports -LocalAddress $plan.LocalAddress -RemoteAddress $plan.RemoteSubnet -InterfaceAlias $adapter.InterfaceAlias -Profile Any | Out-Null
    Write-Output "LAN access configured: http://$($plan.LocalAddress):$HttpPort"
} catch {
    if (-not (Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue)) { Remove-Item -LiteralPath $statePath }
    throw
}
