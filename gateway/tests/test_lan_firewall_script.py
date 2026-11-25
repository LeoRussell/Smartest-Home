"""Validate narrow Windows LAN plans without calling any firewall cmdlets."""

import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "Windows PowerShell planning checks")
class LanFirewallPlanningTests(unittest.TestCase):
    def execute(self, script):
        root = Path(__file__).resolve().parents[2]
        with TemporaryDirectory(prefix="gateway-firewall-plan-") as temporary:
            path = Path(temporary) / "check.ps1"
            path.write_text("param([string]$ModulePath)\n$ErrorActionPreference='Stop'\n"
                            "Import-Module $ModulePath -Force\n" + script, encoding="utf-8")
            completed = subprocess.run([
                "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(path),
                str(root / "deploy/lan-firewall-plan.psm1"),
            ], capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            return json.loads(completed.stdout)

    def test_plan_limits_access_to_exact_host_subnet_and_two_ports(self):
        plans = self.execute("""
            $plans = @(
                (New-LanFirewallPlan -IPv4Address 192.168.0.104 -PrefixLength 24 -Ports 8080,8765),
                (New-LanFirewallPlan -IPv4Address 10.0.0.1 -PrefixLength 8 -Ports 1,65535),
                (New-LanFirewallPlan -IPv4Address 172.16.0.1 -PrefixLength 12 -Ports 3,2)
            )
            ConvertTo-Json -InputObject $plans -Depth 5
        """)
        self.assertEqual(plans, [
            {"LocalAddress": "192.168.0.104", "RemoteSubnet": "192.168.0.0/24", "Ports": ["8080", "8765"]},
            {"LocalAddress": "10.0.0.1", "RemoteSubnet": "10.0.0.0/8", "Ports": ["1", "65535"]},
            {"LocalAddress": "172.16.0.1", "RemoteSubnet": "172.16.0.0/12", "Ports": ["2", "3"]},
        ])

    def test_public_or_overbroad_subnets_and_invalid_ports_are_rejected(self):
        failures = self.execute("""
            $cases = @(
                @{IPv4Address='8.8.8.8'; PrefixLength=24; Ports=@(8080,8765)},
                @{IPv4Address='192.168.0.104'; PrefixLength=1; Ports=@(8080,8765)},
                @{IPv4Address='192.168.0.0'; PrefixLength=24; Ports=@(8080,8765)},
                @{IPv4Address='192.168.0.255'; PrefixLength=24; Ports=@(8080,8765)},
                @{IPv4Address='::1'; PrefixLength=24; Ports=@(8080,8765)},
                @{IPv4Address='192.168.0.104'; PrefixLength=24; Ports=@(8080,8080)},
                @{IPv4Address='192.168.0.104'; PrefixLength=24; Ports=@(0,8765)}
            )
            $failed = @()
            foreach ($case in $cases) {
                try { New-LanFirewallPlan @case | Out-Null; $failed += $false }
                catch { $failed += $true }
            }
            ConvertTo-Json -InputObject $failed
        """)
        self.assertEqual(failures, [True] * 7)

    def test_user_scoped_blocks_match_actual_token_and_unknown_policies_fail_closed(self):
        matches = self.execute("""
            $token = @('S-1-5-21-1-2-3-1001', 'S-1-5-32-545', 'S-1-1-0')
            $results = @(
                (Test-LanFirewallUserScope -LocalUser $null -TokenSids $token),
                (Test-LanFirewallUserScope -LocalUser 'O:LSD:(A;;CC;;;S-1-5-21-1-2-3-1001)' -TokenSids $token),
                (Test-LanFirewallUserScope -LocalUser 'O:LSD:(A;;CC;;;S-1-5-32-545)' -TokenSids $token),
                (Test-LanFirewallUserScope -LocalUser 'O:LSD:(A;;CC;;;S-1-5-21-1-2-3-1004)' -TokenSids $token),
                (Test-LanFirewallUserScope -LocalUser 'O:LSD:(A;;CC;;;S-1-5-21-1-2-3-1004)(A;;CC;;;S-1-1-0)' -TokenSids $token),
                (Test-LanFirewallUserScope -LocalUser 'O:LSD:(D;;CC;;;S-1-5-21-1-2-3-1004)' -TokenSids $token),
                (Test-LanFirewallUserScope -LocalUser 'not a descriptor' -TokenSids $token),
                (Test-LanFirewallUserScope -LocalUser 'O:LSD:(A;;DC;;;S-1-5-21-1-2-3-1004)' -TokenSids $token)
            )
            ConvertTo-Json -InputObject $results
        """)
        self.assertEqual(matches, [True, True, True, False, True, True, True, True])

    def test_optional_firewall_query_accepts_only_known_empty_results_and_keeps_rules(self):
        result = self.execute("""
            $empty = @(Invoke-LanFirewallQuery -Query {}).Count
            $missing = @()
            foreach ($id in @(
                'CmdletizationQuery_NotFound,Get-NetFirewallRule',
                'CmdletizationQuery_NotFound_InstanceID,Get-NetFirewallRule'
            )) {
                $failure = [System.Management.Automation.ErrorRecord]::new(
                    [System.Exception]::new('synthetic missing rule'), $id,
                    [System.Management.Automation.ErrorCategory]::ObjectNotFound, $null)
                $missing += @(Invoke-LanFirewallQuery -Query { throw $failure }).Count
            }
            $rules = @(
                [PSCustomObject]@{ Name='Python block'; Action='Block'; Program='python.exe' },
                [PSCustomObject]@{ Name='Any application block'; Action='Block'; Program='Any' }
            )
            $found = @(Invoke-LanFirewallQuery -Query { $rules })
            ConvertTo-Json -Depth 5 -InputObject @{
                Empty=$empty; Missing=$missing; Rules=$found
                SameObjects=@(
                    [object]::ReferenceEquals($rules[0], $found[0]),
                    [object]::ReferenceEquals($rules[1], $found[1])
                )
            }
        """)
        self.assertEqual(result["Empty"], 0)
        self.assertEqual(result["Missing"], [0, 0])
        self.assertEqual(result["Rules"], [
            {"Name": "Python block", "Action": "Block", "Program": "python.exe"},
            {"Name": "Any application block", "Action": "Block", "Program": "Any"},
        ])
        self.assertEqual(result["SameObjects"], [True, True])

    def test_optional_firewall_query_preserves_unexpected_errors(self):
        results = self.execute("""
            $results = @()
            foreach ($id in @(
                'AccessDenied,Get-NetFirewallRule',
                'CimException,Get-NetFirewallRule',
                'ServiceUnavailable,Get-NetFirewallRule',
                'CmdletizationQuery_NotFound_Name,Get-NetFirewallRule',
                'CmdletizationQuery_NotFound,Get-OtherCommand',
                'CmdletizationQuery_NotFound,Get-NetFirewallRuleUnexpected'
            )) {
                $target = [PSCustomObject]@{ Name='synthetic firewall query' }
                $exception = [System.UnauthorizedAccessException]::new('synthetic read failure')
                $failure = [System.Management.Automation.ErrorRecord]::new(
                    $exception, $id, [System.Management.Automation.ErrorCategory]::ObjectNotFound,
                    $target)
                try {
                    Invoke-LanFirewallQuery -Query { throw $failure } | Out-Null
                    $results += @{ Id=$id; Rethrown=$false }
                } catch {
                    $results += @{
                        Id=$_.FullyQualifiedErrorId; Rethrown=$true
                        SameException=[object]::ReferenceEquals($_.Exception, $exception)
                        SameTarget=[object]::ReferenceEquals($_.TargetObject, $target)
                        Category=$_.CategoryInfo.Category.ToString()
                    }
                }
            }
            ConvertTo-Json -Depth 5 -InputObject $results
        """)
        expected_ids = [
            "AccessDenied,Get-NetFirewallRule",
            "CimException,Get-NetFirewallRule",
            "ServiceUnavailable,Get-NetFirewallRule",
            "CmdletizationQuery_NotFound_Name,Get-NetFirewallRule",
            "CmdletizationQuery_NotFound,Get-OtherCommand",
            "CmdletizationQuery_NotFound,Get-NetFirewallRuleUnexpected",
        ]
        self.assertEqual(results, [
            {"Id": error_id, "Rethrown": True, "SameException": True,
             "SameTarget": True, "Category": "ObjectNotFound"}
            for error_id in expected_ids
        ])


if __name__ == "__main__":
    unittest.main()
