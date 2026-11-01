"""Universal device configuration delivery and acknowledgement lifecycle."""

import asyncio
import hashlib
import json
from websockets.exceptions import ConnectionClosed
from device_configuration import validate_device_configuration


class ConfigurationMixin:
    def cancel_configuration_apply(self, device_id, pending=False):
        task = self.configuration_apply_tasks.pop(device_id, None)
        if task is not None and not task.done():
            task.cancel()
        if pending:
            self.configuration_confirmed_devices.discard(device_id)
            record = self.load_device_configuration(device_id)
            if record is not None and record["status"] == "applying":
                self.update_device_configuration_status(device_id, "pending")
                self.write_device_configuration_export(device_id)
        return task


    def device_configuration_is_pending(self, device_id):
        return device_id in self.configuration_apply_tasks or (
            device_id in self.deferred_connection_automations
            and self.device_metadata.get(device_id, {}).get("device_profile") == "universal"
        )


    async def resume_device_configuration(self, device_id):
        if device_id not in self.devices or device_id in self.configuration_apply_tasks or device_id in self.pending_device_schemas:
            return
        connected_event_pending = device_id in self.deferred_connection_automations
        if connected_event_pending:
            if device_id not in self.configuration_confirmed_devices:
                return
            parameters = self.device_metadata.get(device_id, {}).get("schema", {}).get("parameters", {})
            if not all(self.parameter_value_is_fresh(device_id, parameter) for parameter in parameters):
                return
            self.deferred_connection_automations.discard(device_id)
        await self.deliver_queued_commands(device_id)
        if connected_event_pending and self.device_is_connected(device_id):
            self.fire_automation_event("device_connected", device_id)


    async def expire_configuration_apply(self, device_id, version, config_hash, websocket):
        task = asyncio.current_task()
        try:
            await asyncio.sleep(self.CONFIGURATION_APPLY_TIMEOUT_SECONDS)
            record = self.load_device_configuration(device_id)
            if (
                self.configuration_apply_tasks.get(device_id) is not task
                or self.devices.get(device_id) is not websocket
                or record is None
                or record["config_version"] != version
                or record["config_hash"] != config_hash
                or record["status"] != "applying"
            ):
                return
            error = "Устройство не подтвердило конфигурацию вовремя. Повторите отправку или проверьте подключение."
            self.update_device_configuration_status(device_id, "failed", error=error)
            await self.finish_pending_commands_for_device(device_id, "unavailable", error, retry=False)
            self.write_device_configuration_export(device_id)
            self.save_device_event(device_id, "configuration", f"Не получено подтверждение конфигурации версии {version}.")
            await self.send_device_list()
            await self.send_event_history()
            await self.broadcast({
                "type": "device_configuration_apply_result", "device": device_id,
                "config_version": version, "success": False, "status": "failed",
                "message": error
            })
        finally:
            if self.configuration_apply_tasks.get(device_id) is task:
                self.configuration_apply_tasks.pop(device_id, None)


    async def send_device_configuration(self, device_id, websocket=None):
        metadata = self.device_metadata.get(device_id) or self.load_device_metadata(device_id)
        if metadata is None or metadata.get("device_profile") != "universal":
            return False
        target = websocket or self.devices.get(device_id)
        if target is None:
            return False
        record = self.load_device_configuration(device_id)
        if record is None:
            await self.send_json(target, {"type": "device_config_request"})
            return False
        self.cancel_configuration_apply(device_id)
        self.configuration_confirmed_devices.discard(device_id)
        self.update_device_configuration_status(device_id, "applying")
        task = asyncio.create_task(self.expire_configuration_apply(
            device_id, record["config_version"], record["config_hash"], target
        ))
        self.configuration_apply_tasks[device_id] = task
        try:
            await self.send_json(target, {
                "type": "device_config_apply",
                "device": self.firmware_identity_for(device_id),
                "config_version": record["config_version"],
                "config_hash": record["config_hash"],
                "configuration": record["config"]
            })
        except (ConnectionClosed, OSError):
            if self.configuration_apply_tasks.get(device_id) is task:
                self.cancel_configuration_apply(device_id, pending=True)
            await self.send_device_list()
            return False
        await self.send_device_list()
        return True


    async def handle_device_configuration_result(self, device_id, wire_device_id, message):
        if message.get("device") != wire_device_id:
            return
        version = message.get("config_version")
        config_hash = message.get("config_hash")
        success = message.get("success")
        error = message.get("error")
        metadata = self.device_metadata.get(device_id) or self.load_device_metadata(device_id)
        record = self.load_device_configuration(device_id)

        if (
            metadata is None
            or metadata.get("device_profile") != "universal"
            or type(version) is not int
            or type(success) is not bool
            or not isinstance(config_hash, str)
            or not isinstance(error, (str, type(None)))
            or (isinstance(error, str) and len(error) > 240)
        ):
            return

        if version == 0 and record is None:
            _, expected_schema, validation_error = validate_device_configuration(
                {"format_version": 1, "components": []}, metadata.get("driver_ids", [])
            )
            schema = message.get("schema")
            if (
                not success
                or config_hash != ""
                or validation_error
                or type(message.get("schema_version")) is not int
                or message.get("schema_version") != 1
                or not self.validate_schema(schema)
                or not self.schemas_match(expected_schema, schema)
            ):
                print(f"[CONFIG] {device_id}: invalid empty configuration report.")
                return
            metadata = dict(metadata)
            metadata.update({"schema": expected_schema, "schema_version": 1})
            self.save_device_metadata(device_id, metadata)
            self.configuration_confirmed_devices.add(device_id)
            self.update_device_configuration_status(device_id, "not_configured")
            self.write_device_configuration_export(device_id)
            await self.send_device_list()
            await self.resume_device_configuration(device_id)
            return

        if (
            record is None
            or version != record["config_version"]
            or config_hash != record["config_hash"]
        ):
            return
        self.cancel_configuration_apply(device_id)

        normalized, expected_schema, validation_error = validate_device_configuration(
            record["config"], metadata.get("driver_ids", [])
        )
        reported_schema = message.get("schema")
        if success and (
            validation_error
            or not self.validate_schema(reported_schema)
            or not self.schemas_match(expected_schema, reported_schema)
            or type(message.get("schema_version")) is not int
            or message.get("schema_version") != version
        ):
            success = False
            error = validation_error or "Schema устройства не соответствует принятой конфигурации."

        if success:
            metadata = dict(metadata)
            metadata.update({
                "schema": expected_schema,
                "schema_version": version,
                "device_profile": "universal"
            })
            self.save_device_metadata(device_id, metadata)
            self.update_device_configuration_status(
                device_id, "applied", applied_at=self.current_time()
            )
            self.fresh_device_parameters[device_id] = set()
            self.configuration_confirmed_devices.add(device_id)
            event = f"Аппаратная конфигурация версии {version} применена устройством."
            print(f"[CONFIG] {device_id}: applied configuration v{version}.")
        else:
            error = error or "Устройство отклонило аппаратную конфигурацию."
            self.update_device_configuration_status(device_id, "failed", error=error)
            await self.finish_pending_commands_for_device(device_id, "unavailable", error, retry=False)
            event = f"Ошибка применения аппаратной конфигурации версии {version}: {error}"
            print(f"[CONFIG] {device_id}: configuration v{version} rejected: {error}")

        self.write_device_configuration_export(device_id)
        self.save_device_event(device_id, "configuration", event)
        await self.send_device_list()
        await self.send_event_history()
        await self.send_database_stats()
        await self.broadcast({
            "type": "device_configuration_apply_result",
            "success": success,
            "device": device_id,
            "config_version": version,
            "status": "applied" if success else "failed",
            "message": "Конфигурация применена." if success else error
        })
        if success:
            await self.resume_device_configuration(device_id)


    async def handle_device_configuration_report(self, device_id, wire_device_id, message):
        """Reconcile a configuration already stored in ESP32 NVS after DB loss."""
        metadata = self.device_metadata.get(device_id) or self.load_device_metadata(device_id)
        if (
            metadata is None
            or metadata.get("device_profile") != "universal"
            or message.get("device") != wire_device_id
            or type(message.get("config_version")) is not int
            or not 0 <= message["config_version"] <= self.MAX_CONFIGURATION_VERSION
            or type(message.get("schema_version")) is not int
            or not isinstance(message.get("config_hash"), str)
        ):
            return

        version = message["config_version"]
        if version == 0:
            await self.handle_device_configuration_result(device_id, wire_device_id, {
                "device": wire_device_id,
                "config_version": 0,
                "config_hash": message["config_hash"],
                "success": True,
                "schema_version": message["schema_version"],
                "schema": message.get("schema")
            })
            return
        if version < 1:
            return

        # A saved Gateway configuration is authoritative. The report is only used
        # to recover an empty Gateway configuration table.
        if self.load_device_configuration(device_id) is not None:
            await self.send_device_configuration(device_id)
            return

        normalized, expected_schema, validation_error = validate_device_configuration(
            message.get("configuration"), metadata.get("driver_ids", [])
        )
        reported_schema = message.get("schema")
        config_hash = message["config_hash"]
        if validation_error:
            error = validation_error
        else:
            canonical = json.dumps(
                normalized, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False
            )
            expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            error = None
            if config_hash != expected_hash:
                error = "Хэш аппаратной конфигурации не совпадает с её содержимым."
            elif (
                message["schema_version"] != version
                or not self.validate_schema(reported_schema)
                or not self.schemas_match(expected_schema, reported_schema)
            ):
                error = "Schema устройства не соответствует сохранённой конфигурации."
        if error:
            print(f"[CONFIG] {device_id}: stored configuration report rejected: {error}")
            self.save_device_event(
                device_id,
                "configuration",
                f"Gateway не восстановил конфигурацию из ESP32: {error}"
            )
            await self.send_event_history()
            await self.broadcast({
                "type": "device_configuration_apply_result",
                "success": False,
                "device": device_id,
                "config_version": version,
                "status": "failed",
                "message": error
            })
            return

        timestamp = self.current_time()
        with self.database_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("""
                INSERT INTO device_configurations (
                    device_id, config_version, config_json, config_hash,
                    status, last_error, updated_at, applied_at
                ) VALUES (?, ?, ?, ?, 'applied', NULL, ?, ?)
            """, (device_id, version, canonical, config_hash, timestamp, timestamp))
            conn.execute("""
                INSERT INTO device_configuration_history (
                    device_id, config_version, config_json, config_hash,
                    created_at, status
                ) VALUES (?, ?, ?, ?, ?, 'applied')
            """, (device_id, version, canonical, config_hash, timestamp))

        metadata = dict(metadata)
        metadata.update({"schema": expected_schema, "schema_version": version})
        self.save_device_metadata(device_id, metadata)
        self.update_device_configuration_status(device_id, "applied", applied_at=timestamp)
        self.configuration_confirmed_devices.add(device_id)
        self.write_device_configuration_export(device_id)
        self.save_device_event(
            device_id,
            "configuration",
            f"Gateway восстановил аппаратную конфигурацию версии {version} из ESP32."
        )
        await self.send_device_list()
        await self.send_event_history()
        await self.send_database_stats()
        await self.broadcast({
            "type": "device_configuration_apply_result",
            "success": True,
            "device": device_id,
            "config_version": version,
            "status": "applied",
            "message": f"Конфигурация версии {version} восстановлена из устройства."
        })
        await self.resume_device_configuration(device_id)
