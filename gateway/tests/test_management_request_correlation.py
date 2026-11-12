"""An identity acknowledgement must identify the exact submitted request."""

from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import Gateway


class ManagementRequestCorrelationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.gateway = Gateway(Path(temporary.name) / "gateway.db")
        self.gateway.init_db()
        self.gateway.save_device_metadata("lamp", {"name": "Lamp", "schema": {"parameters": {}, "actions": {}}})
        for method in ("send_json", "send_device_list", "send_event_history",
                       "send_command_queue_state", "send_parameter_ui_preferences", "send_database_stats"):
            setattr(self.gateway, method, AsyncMock())

    async def test_saved_identity_echoes_request_id_and_persists_the_submitted_name(self):
        await self.gateway.handle_browser_message(object(), {
            "action": "update_device_identity", "device": "lamp", "new_device_id": "desk-lamp",
            "display_name": "Desk lamp", "request_id": "identity-current",
        })
        result = self.gateway.send_json.call_args_list[0].args[1]
        self.assertEqual(result["type"], "device_identity_result")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["request_id"], "identity-current")
        self.assertEqual(result["device"], "lamp")
        self.assertEqual(result["new_device_id"], "desk-lamp")
        with self.gateway.database_connection() as connection:
            self.assertEqual(connection.execute("SELECT device_id, display_name FROM devices").fetchall(),
                             [("desk-lamp", "Desk lamp")])

    async def test_rejected_identity_retains_request_id_and_does_not_change_the_device(self):
        await self.gateway.handle_browser_message(object(), {
            "action": "update_device_identity", "device": "lamp", "new_device_id": "invalid id",
            "display_name": "Changed name", "request_id": "identity-rejected",
        })
        result = self.gateway.send_json.call_args.args[1]
        self.assertFalse(result["success"], result)
        self.assertEqual(result["request_id"], "identity-rejected")
        self.assertEqual(result["new_device_id"], "lamp")
        with self.gateway.database_connection() as connection:
            self.assertEqual(connection.execute("SELECT device_id, display_name FROM devices").fetchall(),
                             [("lamp", None)])
        self.gateway.send_device_list.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
