"""Console mistakes and damaged legacy state must not stop the Gateway."""

import io
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main as gateway


class ConsoleRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_tolerates_non_object_and_invalid_saved_states(self):
        with TemporaryDirectory() as directory, patch.multiple(
            gateway,
            DB_PATH=Path(directory) / "gateway.db",
            DEVICE_CONFIGURATION_EXPORT_DIR=Path(directory) / "schemas",
            GATEWAY_SESSION_STARTED_AT=None,
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                gateway.init_db()
                with gateway.database_connection() as connection:
                    connection.executemany(
                        "INSERT INTO devices (device_id, state_json, led, connected) "
                        "VALUES (?, ?, 1, 0)",
                        [(f"legacy-{index}", value) for index, value in enumerate(
                            ["null", "[]", "7", '"text"', "invalid{"]
                        )],
                    )
                with patch("builtins.input", side_effect=["history", "exit"]):
                    await gateway.console_loop()
            for index in range(5):
                self.assertIn(f"legacy-{index}", output.getvalue())
            self.assertIn("'led': True", output.getvalue())

    async def test_invalid_console_json_does_not_block_subsequent_commands(self):
        deep_json = "[" * 2000 + "0" + "]" * 2000
        commands = [
            "use lamp",
            "set led " + "9" * 5000,
            "set led " + deep_json,
            "set led true",
            "call blink " + deep_json,
            'call blink {"count": 2}',
            "exit",
        ]
        output = io.StringIO()
        with (
            patch("builtins.input", side_effect=commands),
            patch.object(gateway, "dispatch_device_command", new_callable=AsyncMock) as set_value,
            patch.object(gateway, "dispatch_device_action", new_callable=AsyncMock) as call_action,
            redirect_stdout(output),
        ):
            await gateway.console_loop()
        set_value.assert_awaited_once_with("lamp", "led", True)
        call_action.assert_awaited_once_with("lamp", "blink", arguments={"count": 2})
        self.assertIn("Значение должно быть корректным JSON", output.getvalue())
        self.assertIn("Аргументы должны быть корректным JSON", output.getvalue())


if __name__ == "__main__":
    unittest.main()
