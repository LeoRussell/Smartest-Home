"""Isolated UI checks with a real browser and mocked Gateway: python web/tests/browser_smoke.py."""

import argparse
import asyncio
import base64
import functools
from http.client import HTTPException
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory, TemporaryFile
from threading import Thread
from time import monotonic
import urllib.error
import urllib.parse
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

    async def ready(self, timeout=30.0):
        deadline = monotonic() + timeout
        while (remaining := deadline - monotonic()) > 0:
            try:
                async with asyncio.timeout(remaining):
                    if await self.evaluate("document.readyState==='complete' && typeof renderDevices==='function' && window.__smokeSockets?.[0]?.readyState===1"):
                        return
            except TimeoutError:
                break
            await asyncio.sleep(min(0.1, max(0, deadline - monotonic())))
        try:
            async with asyncio.timeout(2):
                details = await self.evaluate("""({url:location.href,readyState:document.readyState,
                    renderDevices:typeof renderDevices,errors:window.__smokeErrors||[],
                    sockets:(window.__smokeSockets||[]).map(socket=>socket.readyState),
                    body:document.body?.innerText?.slice(0,300),
                    resources:performance.getEntriesByType('resource').slice(-8).map(item=>({url:item.name,status:item.responseStatus}))})""")
        except Exception as error:
            details = {"diagnostics_error": f"{type(error).__name__}: {error}"}
        raise RuntimeError(f"The interface did not become ready within {timeout:g} seconds. State: {json.dumps(details, ensure_ascii=False)}")

    async def reload(self):
        previous = (await self.call("Page.getFrameTree"))["frameTree"]["frame"]["loaderId"]
        await self.call("Page.reload", ignoreCache=True)
        await self.wait_for_reload(previous)

    async def wait_for_reload(self, previous):
        # Page.reload can return while the previous document is still ready.
        # Wait for its replacement before checking the application functions.
        for _ in range(150):
            current = (await self.call("Page.getFrameTree"))["frameTree"]["frame"]["loaderId"]
            if current != previous:
                await self.ready()
                return
            await asyncio.sleep(0.05)
        raise RuntimeError("The interface did not navigate after reload.")


async def check_gateway_address_form(browser):
    key = "iot-gateway-connection-v1"
    await browser.evaluate(f"localStorage.removeItem({json.dumps(key)})")
    await browser.reload()
    active_url = await browser.evaluate("GatewayConnection.url()")
    # Connection/status redraws must never replace a draft, including after blur.
    draft = await browser.evaluate("""(()=>{
        setActivePage('settings');
        const input=document.getElementById('gateway-address');
        input.focus();input.value='ws://10.64.226.105:8765';input.dispatchEvent(new Event('input',{bubbles:true}));
        GatewayConnection.status(false);GatewayConnection.status(true);
        input.blur();GatewayConnection.status(false);GatewayConnection.languageChanged();
        return input.value;
    })()""")
    if draft != "ws://10.64.226.105:8765":
        raise AssertionError(("Connection redraw replaced the typed Gateway address", draft))
    for host in ("0.0.0.0", "0", "0.0", "0x0", "[::]", "[0:0:0:0:0:0:0:0]"):
        invalid = "ws://" + host + ":8765"
        state = await browser.evaluate("""(()=>{
            const input=document.getElementById('gateway-address');input.value=""" + json.dumps(invalid) + ";" + """
            input.closest('form').requestSubmit();
            const message=document.querySelector('.gateway-connection-settings > .management-feedback:not(.gateway-connection-status)');
            return {input:input.value,active:GatewayConnection.url(),saved:localStorage.getItem('iot-gateway-connection-v1'),error:message.textContent,state:message.dataset.state};
        })()""")
        if state["input"] != invalid or state["active"] != active_url or state["saved"] is not None or state["state"] != "error" or not state["error"]:
            raise AssertionError(("The address form accepted or overwrote a wildcard draft", invalid, state))

    # Use the real form submission and wait for its own automatic reload.
    previous = (await browser.call("Page.getFrameTree"))["frameTree"]["frame"]["loaderId"]
    await browser.evaluate("document.getElementById('gateway-address').value='ws://10.64.226.105:8765';setTimeout(()=>document.getElementById('gateway-address').closest('form').requestSubmit(),0)")
    await browser.wait_for_reload(previous)
    state = await browser.evaluate("({input:document.getElementById('gateway-address').value,stored:JSON.parse(localStorage.getItem('iot-gateway-connection-v1')).url,socket:__smokeSockets[0].url,backup:databaseBackupDownloadUrl('test.sqlite3'),schema:deviceConfigurationExportUrl('lamp')})")
    expected = {
        "input": "ws://10.64.226.105:8765/", "stored": "ws://10.64.226.105:8765/", "socket": "ws://10.64.226.105:8765/",
        "backup": "http://10.64.226.105:8765/gateway-backups/test.sqlite3", "schema": "http://10.64.226.105:8765/gateway-schemas/lamp.json",
    }
    if state != expected:
        raise AssertionError(("Saving the LAN address through its form did not survive reload", state))
    # A rejected edit must also preserve a previously saved, working endpoint.
    state = await browser.evaluate("""(()=>{
        const input=document.getElementById('gateway-address');input.value='ws://0.0.0.0:8765';input.closest('form').requestSubmit();
        return {input:input.value,stored:JSON.parse(localStorage.getItem('iot-gateway-connection-v1')).url,active:GatewayConnection.url()};
    })()""")
    if state != {"input": "ws://0.0.0.0:8765", "stored": expected["stored"], "active": expected["socket"]}:
        raise AssertionError(("A rejected wildcard edit destroyed the saved LAN address", state))
    await browser.reload()
    if await browser.evaluate("document.getElementById('gateway-address').value") != expected["input"]:
        raise AssertionError("The saved LAN address was lost on another page reload")
    await browser.evaluate((Path(__file__).with_name("smoke.js")).read_text(encoding="utf-8"))
    print("Stalled Gateway connection:", await browser.evaluate("runGatewayConnectingSmoke()"))

    for invalid in ("ws://0.0.0.0:8765/", "ws://0:8765/", "ws://0.0:8765/", "ws://[::]:8765/"):
        await browser.evaluate(f"localStorage.setItem({json.dumps(key)},JSON.stringify({{url:{json.dumps(invalid)}}}))")
        await browser.reload()
        state = await browser.evaluate("({input:document.getElementById('gateway-address').value,active:GatewayConnection.url(),saved:localStorage.getItem('iot-gateway-connection-v1'),state:document.querySelector('.gateway-connection-settings > .management-feedback:not(.gateway-connection-status)').dataset.state})")
        if state != {"input": "", "active": active_url, "saved": None, "state": "warning"}:
            raise AssertionError(("An old wildcard setting did not migrate to this page's usable host", invalid, state))

    # Browsers may reject navigating to 0.0.0.0 before loading the interface.
    # Test that page-host policy with the shipped module and native URL parser.
    await browser.evaluate((Path(__file__).with_name("smoke.js")).read_text(encoding="utf-8"))
    print("Gateway endpoint policy:", await browser.evaluate("runGatewayAddressPolicySmoke()"))
    print("Gateway address form: drafts, wildcard errors/migration, saved LAN reload and consistent downloads passed.")


def find_browser(explicit):
    requested = explicit if explicit is not None else os.environ.get("CHROME_PATH")
    if requested is not None:
        if requested and Path(requested).is_file():
            return str(Path(requested).resolve())
        raise RuntimeError(f"The configured browser executable does not exist: {requested!r}")
    candidates = [shutil.which(command) for command in ("google-chrome", "chromium", "chromium-browser", "chrome", "chrome.exe")]
    for folder in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
        if folder:
            candidates.extend((str(Path(folder) / "Google/Chrome/Application/chrome.exe"), str(Path(folder) / "Microsoft/Edge/Application/msedge.exe")))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    raise RuntimeError("Chrome/Chromium/Edge not found. Pass --chrome or set CHROME_PATH.")


def browser_log_tail(path):
    try:
        with path.open("rb") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 8192))
            return log.read().decode("utf-8", errors="replace").strip() or "(empty)"
    except OSError as error:
        return f"(unavailable: {error})"


def read_browser_page(profile, request_timeout):
    lines = (profile / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
    if not lines:
        raise ValueError("The browser has not written its DevTools port yet.")
    port = int(lines[0])
    if not 1 <= port <= 65535:
        raise ValueError("The browser's DevTools port is invalid.")
    # The browser is local; environment HTTP proxies must not intercept this probe.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(f"http://127.0.0.1:{port}/json", timeout=request_timeout) as response:
        pages = json.load(response)
    if isinstance(pages, list):
        for page in pages:
            if not isinstance(page, dict) or page.get("type") != "page" or not isinstance(page.get("webSocketDebuggerUrl"), str):
                continue
            endpoint = urllib.parse.urlsplit(page["webSocketDebuggerUrl"])
            if endpoint.scheme == "ws" and endpoint.hostname in ("127.0.0.1", "localhost", "::1") and endpoint.port == port:
                return page
    raise ValueError("The browser has not published an inspectable page yet.")


async def wait_for_browser(process, profile, timeout=30.0):
    deadline = monotonic() + timeout
    last_error = None
    while True:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"The browser exited during startup (exit code {return_code}).")
        remaining = deadline - monotonic()
        if remaining <= 0:
            detail = f" Last startup error: {type(last_error).__name__}: {last_error}" if last_error else ""
            raise RuntimeError(f"The browser did not become ready within {timeout:g} seconds.{detail}") from last_error
        try:
            # The port file can appear before HTTP is responsive or a page exists.
            # Probe off the event loop and retry only within the shared deadline.
            async with asyncio.timeout(remaining):
                return await asyncio.to_thread(read_browser_page, profile, min(2.0, remaining))
        except (OSError, urllib.error.URLError, HTTPException, ValueError) as error:
            last_error = error
        remaining = deadline - monotonic()
        if remaining > 0:
            await asyncio.sleep(min(0.1, remaining))


async def run(options):
    executable = find_browser(options.chrome)
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(ROOT / "web")))
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    process = None
    try:
        with TemporaryDirectory(prefix="smartest-home-browser-", ignore_cleanup_errors=True) as temporary, TemporaryFile() as browser_log:
            profile = Path(temporary).resolve()
            chrome_log = profile / "chrome_debug.log"
            browser_environment = {**os.environ, "CHROME_LOG_FILE": str(chrome_log)}
            command = [executable, "--headless=new", "--disable-gpu", "--enable-automation", "--enable-logging", "--no-first-run", "--no-default-browser-check", "--remote-allow-origins=*", "--remote-debugging-port=0", f"--user-data-dir={profile}", "about:blank"]
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                command.insert(1, "--no-sandbox")
            print(f"Browser executable: {executable}\nBrowser profile: {profile}", flush=True)
            process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=browser_log, env=browser_environment, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            print(f"Browser process: {process.pid}", flush=True)
            try:
                try:
                    page = await wait_for_browser(process, profile)
                except RuntimeError as error:
                    browser_log.seek(0, os.SEEK_END)
                    browser_log.seek(max(0, browser_log.tell() - 8192))
                    details = browser_log.read().decode("utf-8", errors="replace").strip()
                    raise RuntimeError(f"{error}\nBrowser executable: {executable}\nBrowser profile: {profile}"
                                       f"\nBrowser stderr:\n{details or '(empty)'}"
                                       f"\nChrome debug log:\n{browser_log_tail(chrome_log)}") from error
                async with websockets.connect(page["webSocketDebuggerUrl"], max_size=16 * 1024 * 1024) as websocket:
                    browser = Browser(websocket)
                    await browser.call("Page.enable")
                    await browser.call("Runtime.enable")
                    await browser.call("Emulation.setFocusEmulationEnabled", enabled=True)
                    await browser.call("Page.addScriptToEvaluateOnNewDocument", source=MOCK_SOCKET)
                    await browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
                    await browser.call("Page.navigate", url=f"http://127.0.0.1:{server.server_port}/")
                    await browser.ready()
                    await browser.evaluate((Path(__file__).with_name("smoke.js")).read_text(encoding="utf-8"))
                    print("Interactions:", await browser.evaluate("runGatewaySmoke()"))
                    await browser.evaluate((Path(__file__).with_name("management-smoke.js")).read_text(encoding="utf-8"))
                    print("Management replies:", await browser.evaluate("runManagementSmoke()"))
                    await browser.evaluate((Path(__file__).with_name("c3-smoke.js")).read_text(encoding="utf-8"))
                    print("C3 configuration:", await browser.evaluate("runC3ConfigurationSmoke()"))
                    await browser.evaluate((Path(__file__).with_name("ds18b20-smoke.js")).read_text(encoding="utf-8"))
                    print("DS18B20 configuration:", await browser.evaluate("runDS18B20ConfigurationSmoke()"))
                    await browser.evaluate((Path(__file__).with_name("parameter-freshness-smoke.js")).read_text(encoding="utf-8"))
                    print("Parameter freshness:", await browser.evaluate("runParameterFreshnessSmoke()"))
                    for filename, function in (
                        ("help-smoke.js", "runGatewayHelpSmoke"),
                        ("settings-layout-smoke.js", "runSettingsLayoutSmoke"),
                        ("connections-layout-smoke.js", "runConnectionsLayoutSmoke"),
                        ("ui-polish-smoke.js", "runContextualCardsSmoke"),
                        ("history-chart-smoke.js", "runHistoryChartSmoke"),
                    ):
                        await browser.evaluate((Path(__file__).with_name(filename)).read_text(encoding="utf-8"))
                        print(function + ":", await browser.evaluate(function + "()"))
                    for width in (375, 320):
                        await browser.call("Emulation.setDeviceMetricsOverride", width=width, height=1000, deviceScaleFactor=1, mobile=False)
                        for function in ("runGatewayHelpSmoke", "runSettingsLayoutSmoke", "runConnectionsLayoutSmoke", "runContextualCardsSmoke", "runDS18B20ConfigurationSmoke", "runParameterFreshnessSmoke", "runHistoryChartSmoke"):
                            await browser.evaluate(function + "()")
                    touch_devices = await browser.evaluate("JSON.parse(JSON.stringify(devices))")
                    for width in (375, 320):
                        await browser.call("Emulation.setDeviceMetricsOverride", width=width, height=1000, deviceScaleFactor=1, mobile=False)
                        await browser.call("Emulation.setTouchEmulationEnabled", enabled=True)
                        target = await browser.evaluate("prepareHistoryTouchSmoke()")
                        await browser.call("Input.dispatchTouchEvent", type="touchStart", touchPoints=[target])
                        await browser.call("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
                        touch = await browser.evaluate("new Promise(resolve=>setTimeout(()=>{const tip=document.querySelector('.history-tooltip');const chart=tip.closest('.history-chart').getBoundingClientRect();const box=tip.getBoundingClientRect();resolve({visible:!tip.hidden,value:tip.querySelector('strong').textContent,inside:box.left>=chart.left&&box.right<=chart.right+.5,overflow:document.documentElement.scrollWidth>innerWidth+1})},50))")
                        if not touch["visible"] or "(1)" not in touch["value"] or not touch["inside"] or touch["overflow"]:
                            raise AssertionError(("Native chart touch", width, touch))
                        await browser.call("Emulation.setTouchEmulationEnabled", enabled=False)
                    await browser.evaluate("socket.receive({type:'device_list',devices:" + json.dumps(touch_devices) + "})")
                    print("History period preferences:", await browser.evaluate("runHistoryPeriodPreferenceSmoke()"))
                    print("History chart: desktop, 375/320 px and native touch passed.")
                    await browser.call("Emulation.setDeviceMetricsOverride", width=1440, height=1000, deviceScaleFactor=1, mobile=False)
                    print("Contextual help and modal layouts: desktop, 375 px and 320 px passed.")
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
                    if options.screenshots:
                        await browser.evaluate("uiSettings.language='ru';uiSettings.navigation='top';document.body.classList.remove('nav-side');applyLanguage()")
                        for width in (1440, 320):
                            await browser.call("Emulation.setDeviceMetricsOverride", width=width, height=1000, deviceScaleFactor=1, mobile=False)
                            for kind in ("devices", "settings", "connections", "hardware"):
                                await browser.evaluate(f"prepareUiPolishPreview({json.dumps(kind)})")
                                screenshot = await browser.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
                                (options.screenshots / f"dialog-{kind}-{width}.png").write_bytes(base64.b64decode(screenshot["data"]))
                        await browser.evaluate("GatewayHelp.closeAll();for(const dialog of document.querySelectorAll('dialog[open]'))dialog.close();new Promise(resolve=>setTimeout(resolve,0))")
                    await browser.evaluate("setActivePage('automations');document.querySelector('[data-role=discard-automation-draft]')?.click();document.getElementById('automation-name').value='Recovery smoke';document.getElementById('automation-name').dispatchEvent(new Event('input'));AutomationDraft.flush()")
                    await browser.reload()
                    await browser.ready()
                    if not await browser.evaluate("document.querySelectorAll('.history-tools select')[1].value==='day'"):
                        raise AssertionError("An explicit history period was lost after page reload.")
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
                    await browser.reload()
                    await browser.ready()
                    state = await browser.evaluate("({socket:__smokeSockets[0].url,backup:databaseBackupDownloadUrl('test.sqlite3'),schema:deviceConfigurationExportUrl('lamp')})")
                    if state != {"socket": "ws://127.0.0.1:18765/", "backup": "http://127.0.0.1:18765/gateway-backups/test.sqlite3", "schema": "http://127.0.0.1:18765/gateway-schemas/lamp.json"}:
                        raise AssertionError(state)
                    if not await browser.evaluate("setActivePage('automations');document.querySelector('[data-role=restore-automation-draft]').hidden"):
                        raise AssertionError("A draft from another Gateway appeared in the editor")
                    print("Persisted Gateway address: WebSocket, backup and Schema URLs agree.")
                    await browser.evaluate("localStorage.removeItem('iot-gateway-connection-v1');localStorage.removeItem('iot-gateway-notifications-v2:'+encodeURIComponent('ws://'+location.hostname+':8765/'));localStorage.setItem('iot-gateway-notifications-v1',JSON.stringify({unread:[],readIds:['42'],lastReadEventId:42}))")
                    await browser.reload()
                    await browser.ready()
                    if not await browser.evaluate("lastReadEventId===42 && readNotificationIds.has('42')"):
                        raise AssertionError("Default Gateway notification read markers did not migrate")
                    print("Existing notification read markers: migrated only to the default Gateway.")
                    # A newly opened LAN browser must discover the launcher's
                    # custom WS port without copying localhost from the server.
                    await browser.evaluate("localStorage.removeItem('iot-gateway-connection-v1');localStorage.removeItem('iot-gateway-notifications-v2:'+encodeURIComponent('ws://'+location.hostname+':18777/'))")
                    QuietHandler.runtime_config = {"gateway_port": 18777}
                    await browser.reload()
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
                    await browser.reload()
                    await browser.ready()
                    override = await browser.evaluate("({socket:GatewayConnection.url(),default:GatewayConnection.isDefault()})")
                    if override != {"socket": "ws://127.0.0.1:18765/", "default": False}:
                        raise AssertionError(("Explicit Gateway address lost precedence over launch config", override))
                    await browser.evaluate("localStorage.setItem('iot-gateway-connection-v1',JSON.stringify({url:'ws://'+location.hostname+':8765/'}));localStorage.removeItem('iot-gateway-notifications-v2:'+encodeURIComponent('ws://'+location.hostname+':8765/'))")
                    await browser.reload()
                    await browser.ready()
                    if not await browser.evaluate("!GatewayConnection.isDefault() && GatewayConnection.isLegacyDefault() && lastReadEventId===42 && readNotificationIds.has('42')"):
                        raise AssertionError("Explicit legacy endpoint did not retain notification migration on a custom-port page")
                    await browser.evaluate("localStorage.removeItem('iot-gateway-connection-v1')")
                    for invalid_port in (0, 65536, "18777", True):
                        QuietHandler.runtime_config = {"gateway_port": invalid_port}
                        await browser.reload()
                        await browser.ready()
                        if await browser.evaluate("GatewayConnection.url()") != "ws://127.0.0.1:8765/":
                            raise AssertionError(("Invalid runtime port was trusted", invalid_port))
                    print("Launcher runtime port: discovered, immutable, explicit override preserved; invalid ports use the static fallback.")
                    QuietHandler.runtime_config = None
                    await check_gateway_address_form(browser)
                    modes_source = (Path(__file__).with_name("workspace-modes-smoke.js")).read_text(encoding="utf-8")
                    await browser.evaluate(modes_source)
                    await browser.evaluate("WorkspaceModesSmoke.seed()")
                    await browser.reload()
                    await browser.ready()
                    await browser.evaluate(modes_source)
                    print("Overview mode migration:", await browser.evaluate("WorkspaceModesSmoke.migration()"))
                    await browser.reload()
                    await browser.ready()
                    await browser.evaluate(modes_source)
                    print("Overview mode controls:", await browser.evaluate("WorkspaceModesSmoke.interactions()"))
                    for language in ("ru", "en"):
                        await browser.evaluate(f"uiSettings.language={json.dumps(language)};applyLanguage()")
                        for width in (1440, 375, 320):
                            await browser.call("Emulation.setDeviceMetricsOverride", width=width, height=1000, deviceScaleFactor=1, mobile=False)
                            print("Overview mode layout:", await browser.evaluate("WorkspaceModesSmoke.layout()"))
                            if options.screenshots and language == "ru":
                                await browser.evaluate("document.querySelector('[data-workspace-control=mode-select]').closest('.workspace-tile').scrollIntoView({block:'center'});new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))")
                                screenshot = await browser.call("Page.captureScreenshot", format="png", captureBeyondViewport=False)
                                (options.screenshots / f"overview-modes-{width}.png").write_bytes(base64.b64decode(screenshot["data"]))
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
