"""Backup replies correlate to requests without inventing IDs on broadcasts."""

import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import Gateway


class Browser:
    def __init__(self):
        self.messages = []

    async def send(self, document):
        self.messages.append(json.loads(document))


class BackupProtocolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.gateway = Gateway(Path(directory.name) / "gateway.db")
        self.gateway.init_db()
        self.browser = Browser()
        self.observer = Browser()
        self.gateway.browser_clients.update((self.browser, self.observer))

    async def request(self, action, request_id="backup-request", **fields):
        await self.gateway.handle_browser_message(self.browser, {
            "action": action, "request_id": request_id, **fields,
        })

    async def test_get_and_create_echo_ids_but_broadcast_lists_do_not(self):
        await self.request("get_database_backups", "list-1")
        self.assertEqual(self.browser.messages[-1]["request_id"], "list-1")
        await self.request("create_database_backup", "create-1")
        result = next(message for message in self.browser.messages if message["type"] == "database_backup_result")
        self.assertTrue(result["success"], result)
        self.assertEqual(result["request_id"], "create-1")
        self.assertTrue((self.gateway.DATABASE_BACKUP_DIR / result["backup"]["backup_id"]).is_file())
        self.assertEqual(self.observer.messages[-1]["type"], "database_backups")
        self.assertNotIn("request_id", self.observer.messages[-1])
        await self.request("restore_database_backup", "restore-1", backup_id="invalid")
        result = self.browser.messages[-1]
        self.assertEqual(result["type"], "database_backup_restore_result")
        self.assertFalse(result["success"])
        self.assertEqual(result["request_id"], "restore-1")

    async def test_filesystem_failures_return_correlated_errors_without_disconnect(self):
        with patch.object(self.gateway, "list_database_backups", side_effect=OSError("directory unavailable")):
            await self.request("get_database_backups", "list-failed")
        failure = self.browser.messages[-1]
        self.assertEqual((failure["type"], failure["action"], failure["request_id"]),
                         ("error", "get_database_backups", "list-failed"))
        with patch.object(self.gateway, "create_database_backup", side_effect=OSError("disk unavailable")):
            await self.request("create_database_backup", "create-failed")
        failure = self.browser.messages[-1]
        self.assertFalse(failure["success"])
        self.assertEqual(failure["request_id"], "create-failed")
        # The browser can retry normally after the filesystem becomes available.
        await self.request("get_database_backups", "recovered")
        self.assertEqual((self.browser.messages[-1]["type"], self.browser.messages[-1]["request_id"]),
                         ("database_backups", "recovered"))

    async def test_invalid_request_ids_do_not_create_or_restore_backups(self):
        with patch.object(self.gateway, "create_database_backup") as create, \
                patch.object(self.gateway, "restore_database_backup", new=AsyncMock()) as restore:
            for request_id in ([], "", "x" * 129, True):
                await self.request("create_database_backup", request_id)
                self.assertEqual(self.browser.messages[-1]["code"], "invalid_request_id")
                await self.request("restore_database_backup", request_id, backup_id="invalid")
                self.assertEqual(self.browser.messages[-1]["code"], "invalid_request_id")
            create.assert_not_called()
            restore.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
