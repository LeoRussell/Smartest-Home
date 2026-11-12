"""Execute the production firmware cache with fault-injecting host storage.

Requires g++/clang++, an explicit CXX executable or ZIG executable. No PlatformIO,
board connection, real NVS or application database is involved.
"""
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


class FirmwareCommandCacheTests(unittest.TestCase):
    def compiler(self):
        if os.environ.get("ZIG"):
            return [os.environ["ZIG"], "c++"]
        executable = os.environ.get("CXX") or shutil.which("g++") or shutil.which("clang++")
        if not executable:
            self.skipTest("Native C++ compiler unavailable; set CXX or ZIG to run firmware host regression")
        return [executable]

    def build_and_run(self, sources, expected_output, extra_flags=()):
        root = Path(__file__).resolve().parents[2]
        with TemporaryDirectory(prefix="gateway-firmware-cache-") as directory:
            executable = Path(directory) / ("command-cache.exe" if os.name == "nt" else "command-cache")
            built = subprocess.run(self.compiler() + [
                "-std=c++11", "-Wall", "-Wextra", "-Werror", "-O1", "-UNDEBUG",
                "-I", str(root / "esp32/shared/lib/GatewayClient/src"),
                *extra_flags, *(str(root / source) for source in sources), "-o", str(executable),
            ], capture_output=True, text=True, timeout=90)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=10)
            self.assertEqual(ran.returncode, 0, ran.stdout + ran.stderr)
            self.assertIn(expected_output, ran.stdout)

    def test_durable_cache_recovery_and_storage_failures(self):
        self.build_and_run(
            ["esp32/tests/command_cache_host.cpp"], "13 durable command cache scenarios passed"
        )

    def test_profiles_nvs_binding_migration_and_negative_ack(self):
        root = Path(__file__).resolve().parents[2]
        configured = os.environ.get("ARDUINOJSON_INCLUDE_DIR")
        candidates = ([Path(configured)] if configured else []) + [
            root / "esp32/universal/.pio/libdeps/esp32-s3-devkitc-1/ArduinoJson/src",
            root / "esp32/examples/led/.pio/libdeps/esp32-s3-devkitc-1/ArduinoJson/src",
        ]
        include = next((path for path in candidates if (path / "ArduinoJson.h").is_file()), None)
        if include is None:
            self.skipTest("ArduinoJson headers unavailable; build a firmware profile or set ARDUINOJSON_INCLUDE_DIR")
        self.build_and_run(
            ["esp32/tests/command_cache_adapter_host.cpp",
             "esp32/shared/lib/GatewayClient/src/CommandResultCache.cpp"],
            "9 command cache adapter scenarios passed",
            ["-DARDUINOJSON_ENABLE_ARDUINO_STRING=1",
             "-I", str(root / "esp32/tests/host_shims"), "-I", str(include)],
        )


if __name__ == "__main__":
    unittest.main()
