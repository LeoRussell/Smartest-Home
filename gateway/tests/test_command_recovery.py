"""Gateway restart only resumes intact manual commands, never interrupted scenarios."""

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import Gateway


class CommandRecoveryTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gateway = Gateway(Path(directory.name) / "gateway.db")
        self.gateway.init_db()

    def record(self, command_id, kind="parameter", **extra):
        operation = {"kind": kind, "name": "power" if kind == "parameter" else "identify", **extra}
        data = {"command": "set" if kind == "parameter" else "invoke", "command_id": command_id}
        if kind == "parameter":
            operation["value"] = True
            data.update(parameter="power", value=True)
        else:
            operation["arguments"] = {}
            data.update(action="identify", arguments={})
        pending = {"command_id": command_id, "device": "lamp", "operation": operation,
                   "payload": {"type": "command", "device": "lamp", "data": data}}
        self.gateway.create_command_queue_record(pending)
        return pending

    def restarted(self):
        gateway = Gateway(self.gateway.DB_PATH)
        gateway.init_db()
        gateway.load_pending_commands()
        return gateway

    def statuses(self):
        with self.gateway.database_connection() as connection:
            return dict(connection.execute("SELECT command_id, status FROM command_queue"))

    def test_restart_preserves_manual_but_never_replays_scenario_or_bulk(self):
        for kind in ("parameter", "action"):
            self.record("manual-" + kind, kind)
            self.record("automatic-" + kind, kind, automation_id="scenario", automation_run_id="old-run")
            self.record("bulk-" + kind, kind, transient=True)
        with self.gateway.database_connection() as connection:
            connection.execute("UPDATE command_queue SET status = 'sent', attempts = 1")
        restarted = self.restarted()
        self.assertEqual(set(restarted.pending_commands), {"manual-parameter", "manual-action"})
        self.assertEqual(self.statuses(), {
            "manual-parameter": "queued", "manual-action": "queued",
            "automatic-parameter": "unavailable", "automatic-action": "unavailable",
            "bulk-parameter": "unavailable", "bulk-action": "unavailable",
        })

    def test_malformed_persisted_commands_become_failed_instead_of_looping_on_connect(self):
        for index in range(7):
            self.record(f"bad-{index}")
        with self.gateway.database_connection() as connection:
            for index, bad in enumerate(("not json", "[]", "null")):
                connection.execute("UPDATE command_queue SET operation_json = ? WHERE command_id = ?", (bad, f"bad-{index}"))
            connection.execute("UPDATE command_queue SET payload_json = '{}' WHERE command_id = 'bad-3'")
            connection.execute("UPDATE command_queue SET attempts = -1 WHERE command_id = 'bad-4'")
            operation = {"kind": "unknown", "name": "power", "value": True}
            connection.execute("UPDATE command_queue SET operation_json = ? WHERE command_id = 'bad-5'", (json.dumps(operation),))
            operation["kind"] = "parameter"
            operation["value"] = False
            connection.execute("UPDATE command_queue SET operation_json = ? WHERE command_id = 'bad-6'", (json.dumps(operation),))
        restarted = self.restarted()
        self.assertEqual(restarted.pending_commands, {})
        self.assertEqual(set(self.statuses().values()), {"failed"})

    def test_gateway_identity_rename_does_not_invalidate_old_wire_target(self):
        pending = self.record("renamed-command")
        with self.gateway.database_connection() as connection:
            connection.execute("UPDATE command_queue SET device_id = 'renamed-lamp'")
        restarted = self.restarted()
        restored = restarted.pending_commands[pending["command_id"]]
        self.assertEqual(restored["device"], "renamed-lamp")
        # Delivery rewrites the wire target from the currently negotiated identity.
        self.assertEqual(restored["payload"]["device"], "lamp")


if __name__ == "__main__":
    unittest.main()
