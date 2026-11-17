Set-StrictMode -Version Latest

function ConvertTo-IPv4Number([System.Net.IPAddress] $Address) {
    $bytes = $Address.GetAddressBytes()
    return [uint64]$bytes[0] * 16777216 + [uint64]$bytes[1] * 65536 + [uint64]$bytes[2] * 256 + [uint64]$bytes[3]
}

function ConvertFrom-IPv4Number([uint64] $Value) {
    return '{0}.{1}.{2}.{3}' -f (($Value -shr 24) -band 255), (($Value -shr 16) -band 255), (($Value -shr 8) -band 255), ($Value -band 255)
}

function New-LanFirewallPlan {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string] $IPv4Address,
        [Parameter(Mandatory)][ValidateRange(1, 30)][int] $PrefixLength,
        [Parameter(Mandatory)][ValidateNotNullOrEmpty()][int[]] $Ports
    )
    $address = $null
    if (-not [System.Net.IPAddress]::TryParse($IPv4Address, [ref]$address) -or
        $address.AddressFamily -ne [System.Net.Sockets.AddressFamily]::InterNetwork) {
        throw 'Choose an IPv4 address on your local Ethernet/Wi-Fi adapter.'
    }
    $number = ConvertTo-IPv4Number $address
    $private = ($number -ge 167772160 -and $number -le 184549375) -or
        ($number -ge 2886729728 -and $number -le 2887778303) -or
        ($number -ge 3232235520 -and $number -le 3232301055)
    if (-not $private) { throw 'Only a private RFC1918 LAN address is supported.' }
    $size = [uint64][Math]::Pow(2, 32 - $PrefixLength)
    $first = [uint64]([Math]::Floor($number / $size) * $size)
    $last = $first + $size - 1
    $privateBounds = if ($number -le 184549375) { @(167772160, 184549375) }
        elseif ($number -le 2887778303) { @(2886729728, 2887778303) }
        else { @(3232235520, 3232301055) }
    if ($first -lt $privateBounds[0] -or $last -gt $privateBounds[1]) { throw 'The subnet must stay entirely inside the private LAN address range.' }
    if ($number -eq $first -or $number -eq $last) { throw 'The address must be a usable LAN host address.' }
    $selected = @($Ports | Sort-Object -Unique)
    if ($selected.Count -ne 2 -or @($selected | Where-Object { $_ -lt 1 -or $_ -gt 65535 }).Count) {
        throw 'Choose two different TCP ports between 1 and 65535.'
    }
    [PSCustomObject]@{
        LocalAddress = $address.IPAddressToString
        RemoteSubnet = '{0}/{1}' -f (ConvertFrom-IPv4Number $first), $PrefixLength
        Ports = [string[]]$selected
    }
}

function Test-LanFirewallUserScope {
    [CmdletBinding()]
    param([AllowNull()][string] $LocalUser, [string[]] $TokenSids)
    # A simple authorization DACL for other accounts does not match this
    # Python process. Unknown or conditional policies remain potential blocks.
    if ([string]::IsNullOrWhiteSpace($LocalUser) -or $LocalUser -eq 'Any' -or -not $TokenSids) { return $true }
    try { $descriptor = [Security.AccessControl.RawSecurityDescriptor]::new($LocalUser) }
    catch { return $true }
    if (-not $descriptor.DiscretionaryAcl -or -not $descriptor.DiscretionaryAcl.Count) { return $true }
    foreach ($ace in $descriptor.DiscretionaryAcl) {
        if ($ace -isnot [Security.AccessControl.CommonAce] -or $ace.IsCallback -or
            $ace.AceQualifier -ne [Security.AccessControl.AceQualifier]::AccessAllowed -or
            $ace.AceFlags -ne [Security.AccessControl.AceFlags]::None -or $ace.AccessMask -ne 1) { return $true }
        if ($TokenSids -contains $ace.SecurityIdentifier.Value) { return $true }
    }
    return $false
}

Export-ModuleMember -Function New-LanFirewallPlan, Test-LanFirewallUserScope
