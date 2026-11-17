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


if __name__ == "__main__":
    unittest.main()
