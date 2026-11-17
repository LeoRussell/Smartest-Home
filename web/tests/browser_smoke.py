"""Isolated UI checks with a real browser and mocked Gateway: python web/tests/browser_smoke.py."""

import argparse
import asyncio
import base64
import functools
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from threading import Thread
import urllib.request

import websockets


ROOT = Path(__file__).resolve().parents[2]
MOCK_SOCKET = """
window.__smokeErrors = [];
window.addEventListener('error', event => __smokeErrors.push(event.message));
window.addEventListener('unhandledrejection', event => __smokeErrors.push(String(event.reason)));
window.__smokeSockets = [];
window.confirm = () => true;
class SmokeSocket extends EventTarget {
  static CONNECTING=0; static OPEN=1; static CLOSING=2; static CLOSED=3;
  constructor(url) { super(); this.url=url; this.readyState=0; this.sent=[];
    __smokeSockets.push(this); setTimeout(()=>{this.readyState=1;this.dispatchEvent(new Event('open'))},0); }
  send(value) { this.sent.push(JSON.parse(value)); }
  close() { this.readyState=3;this.dispatchEvent(new Event('close')); }
  receive(value) { this.dispatchEvent(new MessageEvent('message',{data:JSON.stringify(value)})); }
}
window.WebSocket=SmokeSocket;
"""


class QuietHandler(SimpleHTTPRequestHandler):
    runtime_config = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.partition("?")[0] == "/gateway-config.js" and self.runtime_config is not None:
            body = ("window.__GATEWAY_CONFIG__ = " + json.dumps(self.runtime_config) + ";\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


class Browser:
    def __init__(self, websocket):
        self.websocket, self.sequence = websocket, 0

    async def call(self, method, **params):
        self.sequence += 1
        identity = self.sequence
        await self.websocket.send(json.dumps({"id": identity, "method": method, "params": params}))
        async with asyncio.timeout(15):
            while True:
                message = json.loads(await self.websocket.recv())
                if message.get("id") == identity:
                    if "error" in message:
                        raise RuntimeError(message["error"])
                    return message.get("result", {})

    async def evaluate(self, expression):
        result = await self.call("Runtime.evaluate", expression=expression, awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in result:
            raise AssertionError(result["exceptionDetails"])
        return result.get("result", {}).get("value")

    async def ready(self):
        for _ in range(100):
            if await self.evaluate("document.readyState==='complete' && typeof renderDevices==='function' && __smokeSockets[0]?.readyState===1"):
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("The interface did not become ready.")


def find_browser(explicit):
    candidates = [explicit, os.environ.get("CHROME_PATH"), shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser")]
    for folder in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        if folder:
            candidates.extend((str(Path(folder) / "Google/Chrome/Application/chrome.exe"), str(Path(folder) / "Microsoft/Edge/Application/msedge.exe")))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise RuntimeError("Chrome/Chromium/Edge not found. Pass --chrome or set CHROME_PATH.")


async def run(options):
    executable = find_browser(options.chrome)
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(ROOT / "web")))
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    process = None
    try:
        with TemporaryDirectory(prefix="smartest-home-browser-", ignore_cleanup_errors=True) as temporary:
            profile = Path(temporary)
            command = [executable, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*", "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"]
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                command.insert(1, "--no-sandbox")
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                for _ in range(150):
                    active_port = profile / "DevToolsActivePort"
                    if active_port.is_file():
                        port = int(active_port.read_text().splitlines()[0])
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as response:
                            pages = json.load(response)
                        page = next(item for item in pages if item["type"] == "page")
                        break
                    if process.poll() is not None:
                        raise RuntimeError("The browser exited during startup.")
                    await asyncio.sleep(0.05)
                else:
                    raise RuntimeError("The browser did not start.")
                async with websockets.connect(page["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as websocket:
                    browser = Browser(websocket)
                    await browser.call("Page.enable")
                    await browser.call("Runtime.enable")
                    await browser.call("Page.addScriptToEvaluateOnNewDocument", source=MOCK_SOCKET)
                    await browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
                    await browser.call("Page.navigate", url=f"http://127.0.0.1:{server.server_port}/")
                    await browser.ready()
                    await browser.evaluate((Path(__file__).with_name("smoke.js")).read_text(encoding="utf-8"))
                    print("Interactions:", await browser.evaluate("runGatewaySmoke()"))
                    await browser.evaluate((Path(__file__).with_name("management-smoke.js")).read_text(encoding="utf-8"))
                    print("Management replies:", await browser.evaluate("runManagementSmoke()"))
                    layouts = 0
                    for language in ("ru", "en"):
                        for navigation in ("top", "side"):
                            await browser.evaluate(f"uiSettings.language={json.dumps(language)};uiSettings.navigation={json.dumps(navigation)};document.body.classList.toggle('nav-side',uiSettings.navigation==='side');applyLanguage()")
                            for width in (1440, 375, 320):
                                await browser.call("Emulation.setDeviceMetricsOverride", width=width, height=1000, deviceScaleFactor=1, mobile=False)
                                for name in ("devices", "overview", "automations", "events", "settings"):
                                    await browser.evaluate(f"setActivePage({json.dumps(name)});new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
                                    state = await browser.evaluate("({width:innerWidth,scroll:document.documentElement.scrollWidth,errors:__smokeErrors})")
                                    if state["scroll"] > width + 1 or state["errors"]:
                                        raise AssertionError((language, navigation, width, name, state))
                                    layouts += 1
                                    if options.screenshots and language == "ru" and navigation == "top" and width in (1440, 320) and name in ("settings", "automations"):
                                        target = ".gateway-connection-settings" if name == "settings" else ".automation-local-draft"
                                        await browser.evaluate(f"document.querySelector({json.dumps(target)})?.scrollIntoView({{block:'center'}})")
                                        screenshot = await browser.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
                                        options.screenshots.mkdir(parents=True, exist_ok=True)
                                        (options.screenshots / f"{name}-{width}.png").write_bytes(base64.b64decode(screenshot["data"]))
                    print(f"Layouts: {layouts}; no overflow or JavaScript errors.")
                    await browser.evaluate("setActivePage('automations');document.querySelector('[data-role=discard-automation-draft]')?.click();document.getElementById('automation-name').value='Recovery smoke';document.getElementById('automation-name').dispatchEvent(new Event('input'));AutomationDraft.flush()")
                    await browser.call("Page.reload", ignoreCache=True)
                    await browser.ready()
                    restored = await browser.evaluate("""(()=>{
                        setActivePage('automations');
                        const restore=document.querySelector('[data-role=restore-automation-draft]');
                        if (!restore || restore.hidden || restore.disabled) throw new Error('Local recovery copy is not available');
                        restore.click();
                        if (document.getElementById('automation-name').value!=='Recovery smoke' || AutomationBlockly.compile().body[0].seconds!==5) throw new Error('Recovery did not restore the original draft');
                        if(socket.sent.some(item=>['save_automation','run_automation','command','invoke_action'].includes(item.action))) throw new Error('Recovery performed a real operation');
                        return true;
                    })()""")
                    print("Local draft recovery after reload:", restored)
                    # Persisted custom endpoints must be shared by WS and downloads.
                    await browser.evaluate("localStorage.setItem('iot-gateway-connection-v1',JSON.stringify({url:'ws://127.0.0.1:18765/'}))")
                    check = await browser.evaluate("({socket:__smokeSockets[0].url,configured:GatewayConnection.url(),download:databaseBackupDownloadUrl('check.sqlite3')})")
                    if check["socket"] != check["configured"] or ":18765/" in check["download"]:
                        raise AssertionError(("Changing storage mixed active Gateway addresses", check))
                    await browser.call("Page.reload", ignoreCache=True)
                    await browser.ready()
                    state = await browser.evaluate("({socket:__smokeSockets[0].url,backup:databaseBackupDownloadUrl('test.sqlite3'),schema:deviceConfigurationExportUrl('lamp')})")
                    if state != {"socket": "ws://127.0.0.1:18765/", "backup": "http://127.0.0.1:18765/gateway-backups/test.sqlite3", "schema": "http://127.0.0.1:18765/gateway-schemas/lamp.json"}:
                        raise AssertionError(state)
                    if not await browser.evaluate("setActivePage('automations');document.querySelector('[data-role=restore-automation-draft]').hidden"):
                        raise AssertionError("A draft from another Gateway appeared in the editor")
                    print("Persisted Gateway address: WebSocket, backup and Schema URLs agree.")
                    await browser.evaluate("localStorage.removeItem('iot-gateway-connection-v1');localStorage.removeItem('iot-gateway-notifications-v2:'+encodeURIComponent('ws://'+location.hostname+':8765/'));localStorage.setItem('iot-gateway-notifications-v1',JSON.stringify({unread:[],readIds:['42'],lastReadEventId:42}))")
                    await browser.call("Page.reload", ignoreCache=True)
                    await browser.ready()
                    if not await browser.evaluate("lastReadEventId===42 && readNotificationIds.has('42')"):
                        raise AssertionError("Default Gateway notification read markers did not migrate")
                    print("Existing notification read markers: migrated only to the default Gateway.")
                    # A newly opened LAN browser must discover the launcher's
                    # custom WS port without copying localhost from the server.
                    await browser.evaluate("localStorage.removeItem('iot-gateway-connection-v1');localStorage.removeItem('iot-gateway-notifications-v2:'+encodeURIComponent('ws://'+location.hostname+':18777/'))")
                    QuietHandler.runtime_config = {"gateway_port": 18777}
                    await browser.call("Page.reload", ignoreCache=True)
                    await browser.ready()
                    custom = await browser.evaluate("({socket:__smokeSockets[0].url,backup:databaseBackupDownloadUrl('test.sqlite3'),schema:deviceConfigurationExportUrl('lamp'),default:GatewayConnection.isDefault(),legacy:GatewayConnection.isLegacyDefault(),readMarker:lastReadEventId,hasLegacyReadId:readNotificationIds.has('42'),help:document.getElementById('gateway-address-help').textContent})")
                    if custom["socket"] != "ws://127.0.0.1:18777/" or custom["backup"] != "http://127.0.0.1:18777/gateway-backups/test.sqlite3" or custom["schema"] != "http://127.0.0.1:18777/gateway-schemas/lamp.json" or not custom["default"] or "18777" not in custom["help"]:
                        raise AssertionError(("Custom launcher port was not applied consistently", custom))
                    if custom["legacy"] or custom["readMarker"] is not None or custom["hasLegacyReadId"]:
                        raise AssertionError(("Legacy notification markers migrated into a custom runtime Gateway", custom))
                    frozen = await browser.evaluate("window.__GATEWAY_CONFIG__.gateway_port=19999;({socket:GatewayConnection.url(),default:GatewayConnection.isDefault()})")
                    if frozen != {"socket": "ws://127.0.0.1:18777/", "default": True}:
                        raise AssertionError(("Runtime mutation changed the active Gateway", frozen))
                    await browser.evaluate("localStorage.setItem('iot-gateway-connection-v1',JSON.stringify({url:'ws://127.0.0.1:18765/'}))")
                    await browser.call("Page.reload", ignoreCache=True)
                    await browser.ready()
                    override = await browser.evaluate("({socket:GatewayConnection.url(),default:GatewayConnection.isDefault()})")
                    if override != {"socket": "ws://127.0.0.1:18765/", "default": False}:
                        raise AssertionError(("Explicit Gateway address lost precedence over launch config", override))
                    await browser.evaluate("localStorage.setItem('iot-gateway-connection-v1',JSON.stringify({url:'ws://'+location.hostname+':8765/'}));localStorage.removeItem('iot-gateway-notifications-v2:'+encodeURIComponent('ws://'+location.hostname+':8765/'))")
                    await browser.call("Page.reload", ignoreCache=True)
                    await browser.ready()
                    if not await browser.evaluate("!GatewayConnection.isDefault() && GatewayConnection.isLegacyDefault() && lastReadEventId===42 && readNotificationIds.has('42')"):
                        raise AssertionError("Explicit legacy endpoint did not retain notification migration on a custom-port page")
                    await browser.evaluate("localStorage.removeItem('iot-gateway-connection-v1')")
                    for invalid_port in (0, 65536, "18777", True):
                        QuietHandler.runtime_config = {"gateway_port": invalid_port}
                        await browser.call("Page.reload", ignoreCache=True)
                        await browser.ready()
                        if await browser.evaluate("GatewayConnection.url()") != "ws://127.0.0.1:8765/":
                            raise AssertionError(("Invalid runtime port was trusted", invalid_port))
                    print("Launcher runtime port: discovered, immutable, explicit override preserved; invalid ports use the static fallback.")
                    QuietHandler.runtime_config = None
                    if not await browser.evaluate("__smokeErrors.length===0"):
                        raise AssertionError(await browser.evaluate("__smokeErrors"))
                    await browser.call("Browser.close")
            finally:
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=10)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--chrome", help="Chrome, Chromium or Edge executable.")
    parser.add_argument("--screenshots", type=Path, help="Optional directory for desktop and mobile UI snapshots.")
    args = parser.parse_args()
    asyncio.run(run(args))
