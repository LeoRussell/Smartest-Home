"""Run the production board limits and PWM route planner without real hardware."""

import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


class UniversalBoardProfileTests(unittest.TestCase):
    def compiler(self):
        if os.environ.get("ZIG"):
            return [os.environ["ZIG"], "c++"]
        executable = os.environ.get("CXX") or shutil.which("g++") or shutil.which("clang++")
        if not executable:
            self.skipTest("Native C++ compiler unavailable; set CXX or ZIG for board/PWM checks")
        return [executable]

    def build_profile(self, macro, profile):
        root = Path(__file__).resolve().parents[2]
        with TemporaryDirectory(prefix="gateway-board-profile-") as temporary:
            executable = Path(temporary) / ("board-profile.exe" if os.name == "nt" else "board-profile")
            built = subprocess.run(self.compiler() + [
                "-std=c++11", "-Wall", "-Wextra", "-Werror", "-O1", "-UNDEBUG",
                f"-D{macro}=1", "-I", str(root / "esp32/universal/include"),
                str(root / "esp32/tests/universal_board_profile_host.cpp"), "-o", str(executable),
            ], capture_output=True, text=True, timeout=90)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn(f"5 universal board and PWM scenarios passed: {profile}", ran.stdout)

    def test_c3_supermini_limits_and_dense_pwm_routes(self):
        self.build_profile("CONFIG_IDF_TARGET_ESP32C3", "esp32-c3-supermini")

    def test_s3_existing_limits_and_retained_pwm_routes(self):
        self.build_profile("CONFIG_IDF_TARGET_ESP32S3", "esp32-s3-devkitc-1")


if __name__ == "__main__":
    unittest.main()
