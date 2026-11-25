"""Windows Model Desk, started by model-desk.cmd in the project root."""
import http.client
import json
import socket
import webbrowser
import services
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

WEB = Path(__file__).parent / "web"
PORT = 8765
ROUTES = {"models", "chat/completions", "audio/transcriptions", "audio/speech", "images/generations", "responses/input_tokens"}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, data, mime):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def data(self, value):
        self.reply(200, json.dumps(value).encode(), "application/json")

    def do_GET(self):
        if self.headers.get("Host") not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            return self.send_error(403)
        if self.path == "/model-desk-info":
            return self.data({"app":"jshi-model-desk","project":str(services.JSHI)})
        if self.path == "/iphone-ca.cer":
            state = services.phone_state(services.listener(services.PORTS["jshi-app"]))
            if not state:
                return self.send_error(404)
            cert = Path(state["directory"]) / "phone-tls" / state["ip"] / "iphone-ca.cer"
            if not cert.is_file():
                return self.send_error(404)
            return self.reply(200,cert.read_bytes(),"application/pkix-cert")
        if self.path == "/launch-options":
            try:
                return self.data(services.launch_options())
            except (OSError, ValueError) as exc:
                return self.reply(500,json.dumps({"error":str(exc)}).encode(),"application/json")
        files = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
        files["/vendor/idb-8.0.3/idb.js"] = ("vendor/idb-8.0.3/idb.js", "text/javascript")
        if self.path == "/favicon.ico":
            return self.reply(204, b"", "image/x-icon")
        if self.path not in files:
            return self.send_error(404)
        name, mime = files[self.path]
        self.reply(200, (WEB / name).read_bytes(), mime)

    def do_POST(self):
        if self.headers.get("Origin") not in (None, f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}") or self.headers.get("Host") not in (f"127.0.0.1:{PORT}", f"localhost:{PORT}"):
            return self.send_error(403)
        if self.path.startswith("/launch/"):
            if self.headers.get("X-Model-Desk") != "1":
                return self.send_error(403)
            try:
                size = int(self.headers.get("Content-Length",0))
                if not 0 < size <= 4096:
                    raise ValueError("启动参数长度无效")
                return self.data(services.launch_app(self.path.split("/")[-1], json.loads(self.rfile.read(size))))
            except (ValueError, OSError) as exc:
                return self.reply(400,json.dumps({"error":str(exc)}).encode(),"application/json")
        route = self.path.removeprefix("/api/")
        if self.path != "/api/" + route or route not in ROUTES:
            return self.send_error(404)
        conn = None
        try:
            base = urlsplit(self.headers.get("X-Model-Endpoint", ""))
            if base.scheme not in ("http", "https") or not base.hostname or base.username is not None or base.query or base.fragment:
                raise ValueError("请使用 HTTP 或 HTTPS 接口")
            size = int(self.headers.get("Content-Length", 0))
            if not 0 <= size <= 32 * 1024 * 1024:
                raise ValueError("文件大小需低于 32 MB")
            body = self.rfile.read(size)
            connection = http.client.HTTPSConnection if base.scheme == "https" else http.client.HTTPConnection
            conn = connection(base.hostname, base.port, timeout=4 if route == "models" else 180)
            conn.request("GET" if route == "models" else "POST", base.path.rstrip("/") + "/" + route, body, {"Content-Type": self.headers.get("Content-Type", "application/json")})
            response = conn.getresponse()
            self.reply(response.status, response.read(), response.getheader("Content-Type", "application/json"))
        except (ValueError, OSError, http.client.HTTPException) as exc:
            message = str(exc) if isinstance(exc, ValueError) else "无法连接模型服务，请确认服务已启动并检查接口地址。"
            self.reply(502, json.dumps({"error": {"message": message}}).encode(), "application/json")
        finally:
            if conn:
                conn.close()


class WorkbenchServer(ThreadingHTTPServer):
    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.allow_reuse_address = False
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main():
    url = f"http://127.0.0.1:{PORT}"
    try:
        server = WorkbenchServer(("127.0.0.1", PORT), Handler)
    except OSError:
        try:
            with services.http.open(url+"/model-desk-info",timeout=2) as response:
                info = json.load(response)
        except (OSError, ValueError):
            info = {}
        if info.get("app")=="jshi-model-desk" and info.get("project")==str(services.JSHI):
            webbrowser.open(url+"/#apps")
            return
        raise SystemExit(f"端口 {PORT} 已被其他应用占用。")
    with server:
        print(f"Model Desk {url} （按 Ctrl+C 退出工作台）", flush=True)
        webbrowser.open(url+"/#apps")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__=="__main__":
    main()
