"""Run the Gateway and its local web interface together: py run.py."""

import argparse
import asyncio
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Thread


ROOT = Path(__file__).resolve().parent


def port_number(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("Port must be between 1 and 65535.")
    return port


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("GATEWAY_HOST", "0.0.0.0"), help="Listen address (default: 0.0.0.0).")
    parser.add_argument("--http-port", type=port_number, default=os.environ.get("GATEWAY_HTTP_PORT", "8080"), help="Web interface port (default: 8080).")
    parser.add_argument("--gateway-port", type=port_number, default=os.environ.get("GATEWAY_PORT", "8765"), help="Gateway WebSocket port (default: 8765).")
    parser.add_argument("--db", type=Path, help="Optional SQLite path; defaults to GATEWAY_DB_PATH or gateway/gateway.db.")
    return parser.parse_args(argv)


def use_project_environment():
    """Use existing dependencies without installing packages or changing PATH."""
    candidates = (ROOT / ".venv/Scripts/python.exe", ROOT / ".venv/bin/python")
    for candidate in candidates:
        if candidate.is_file():
            if Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
                command = [str(candidate), str(Path(__file__).resolve()), *sys.argv[1:]]
                if os.name != "nt":
                    os.execv(str(candidate), command)
                # Windows execv doesn't propagate the replacement's exit code.
                # Share the current console so Ctrl+C reaches both processes,
                # then allow the child to finish its own graceful shutdown.
                process = subprocess.Popen(command)
                try:
                    result = process.wait()
                except KeyboardInterrupt:
                    try:
                        result = process.wait(timeout=15)
                    except (subprocess.TimeoutExpired, KeyboardInterrupt):
                        process.terminate()
                        process.wait(timeout=5)
                        result = 130
                raise SystemExit(result)
            return


class WebHandler(SimpleHTTPRequestHandler):
    def log_request(self, code="-", size="-"):
        if isinstance(code, int) and code >= 400:
            super().log_request(code, size)


async def run_services(options, *, gateway_factory=None, web_directory=None):
    if not options.host.strip():
        raise ValueError("The listen address must not be empty.")
    if options.http_port == options.gateway_port:
        raise ValueError("HTTP and Gateway ports must be different.")
    directory = Path(web_directory) if web_directory is not None else ROOT / "web"
    if not (directory / "index.html").is_file():
        raise ValueError(f"Web interface not found: {directory}")
    if gateway_factory is None:
        sys.path.insert(0, str(ROOT / "gateway"))
        from runtime import Gateway
        gateway_factory = Gateway
    server_type = ThreadingHTTPServer
    if ":" in options.host:
        server_type = type("IPv6WebServer", (ThreadingHTTPServer,), {"address_family": socket.AF_INET6})
    server = server_type((options.host, options.http_port), partial(WebHandler, directory=str(directory)))
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, name="gateway-web", daemon=True)
    thread.start()
    try:
        gateway = gateway_factory(options.db)
        gateway.HOST, gateway.PORT = options.host, options.gateway_port
        host = "localhost" if options.host in ("0.0.0.0", "::") else options.host
        url_host = f"[{host}]" if ":" in host else host
        print(f"Web interface: http://{url_host}:{server.server_port}", flush=True)
        print(f"Gateway: ws://{url_host}:{options.gateway_port}", flush=True)
        if options.gateway_port != 8765:
            print("Set the Gateway address in the web interface settings and match the ESP32 port.", flush=True)
        print("Press Ctrl+C to stop both services.", flush=True)
        await gateway.run()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def main():
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    options = arguments()
    options.host = options.host.strip()
    use_project_environment()
    os.environ["GATEWAY_HOST"] = options.host.strip()
    os.environ["GATEWAY_PORT"] = str(options.gateway_port)
    if options.db is not None:
        os.environ["GATEWAY_DB_PATH"] = str(options.db)
    try:
        sys.path.insert(0, str(ROOT / "gateway"))
        from runtime import run_until_shutdown
        asyncio.run(run_until_shutdown(run_services(options)))
    except KeyboardInterrupt:
        print("\nGateway and web interface stopped.")
    except ModuleNotFoundError as error:
        print(f"Missing dependency: {error.name}. Install gateway/requirements.txt into .venv.", file=sys.stderr)
        return 1
    except (OSError, ValueError) as error:
        print(f"Could not start: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
