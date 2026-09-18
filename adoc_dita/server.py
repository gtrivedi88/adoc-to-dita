"""Loopback-only interface by default. No account, database, upload service or external assets.

Deployment note: this module is designed to be reached only at 127.0.0.1/localhost.
Two environment variables let an operator run it behind a real network listener
(for example, an OpenShift Service/Route) without weakening the default behavior:

  ADOC_DITA_BIND_HOST      Interface to bind to. Defaults to "127.0.0.1" (unchanged
                            behavior). Set to "0.0.0.0" to accept connections that
                            arrive over a container network interface.
  ADOC_DITA_ALLOWED_HOSTS  Comma-separated list of additional Host header values
                            (host[:port]) to accept, on top of the always-allowed
                            127.0.0.1:<port> and localhost:<port>. Leave unset for
                            the original loopback-only behavior.

Neither variable is set by default, so `./adoc-dita serve` is unaffected.
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit, parse_qs

from .cli import parse_attributes
from .context import convert_standalone
from .repository import compare, refs
from .report import zip_report


def serve(port=8765):
    bind_host = os.environ.get("ADOC_DITA_BIND_HOST", "127.0.0.1")
    network_workspace = bind_host not in {"127.0.0.1", "localhost", "::1"}
    extra_hosts = {h.strip() for h in os.environ.get("ADOC_DITA_ALLOWED_HOSTS", "").split(",") if h.strip()}
    token = secrets.token_hex(24)
    jobs = {}
    jobs_lock = threading.Lock()
    conversion_lock = threading.Lock()
    page = (Path(__file__).parent / "index.html").read_text().replace("__TOKEN__", token).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status, data, kind="application/json; charset=utf-8", filename=None):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", f"default-src 'self'; script-src 'nonce-{token}'; style-src 'unsafe-inline'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'")
            if filename:
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.end_headers()
            self.wfile.write(data)

        def allowed(self):
            expected = {f"127.0.0.1:{port}", f"localhost:{port}"} | extra_hosts
            host = self.headers.get("Host", "")
            origin = self.headers.get("Origin")
            origin_host = urlsplit(origin).netloc if origin else None
            if host not in expected or (origin and origin_host not in expected):
                self.respond(403, {"error": "Use the local application origin"})
                return False
            return True

        def do_GET(self):
            if not self.allowed():
                return
            url = urlsplit(self.path)
            if url.path == "/":
                return self.respond(200, page, "text/html; charset=utf-8")
            if url.path == "/favicon.ico":
                return self.respond(204, b"", "image/x-icon")
            if url.path == "/api/job":
                with jobs_lock:
                    job = jobs.get(parse_qs(url.query).get("id", [""])[0])
                    return self.respond(200 if job else 404, job or {"error": "Job not found"})
            if url.path == "/api/download":
                with jobs_lock:
                    job = jobs.get(parse_qs(url.query).get("id", [""])[0])
                    report = job.get("report") if job else None
                if report:
                    return self.respond(200, zip_report(report), "application/zip", "adoc-dita-comparison.zip")
            return self.respond(404, {"error": "Not found"})

        def do_POST(self):
            if not self.allowed():
                return
            if self.headers.get("X-App-Token") != token:
                return self.respond(403, {"error": "Refresh the application page"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > 2 * 1024 * 1024:
                    return self.respond(413, {"error": "Input must be smaller than 2 MB"})
                data = json.loads(self.rfile.read(size))
                if self.path == "/api/refs":
                    if network_workspace and not str(data.get("repository", "")).startswith("https://github.com/"):
                        raise ValueError("Hosted comparisons require a public https://github.com/OWNER/REPO URL")
                    return self.respond(200, {"refs": refs(data["repository"])})
                if self.path == "/api/convert":
                    with conversion_lock:
                        options = dict(filename=data.get("filename", "document.adoc"),
                                       attributes=parse_attributes(data.get("attributes", "").splitlines()), kind=data.get("type", "auto"))
                        attribute_file = data.get("attribute_file", "").strip() or None
                        if network_workspace and attribute_file:
                            raise ValueError("A hosted conversion cannot read a path on the server. Upload the attributes .adoc file instead.")
                        result = convert_standalone(data['source'], **options, attribute_file=attribute_file,
                                                    attribute_text=data.get('attribute_text', ''))
                    return self.respond(200, result)
                if self.path == "/api/compare":
                    repository = data["repository"]
                    if network_workspace and not str(repository).startswith("https://github.com/"):
                        raise ValueError("Hosted comparisons require a public https://github.com/OWNER/REPO URL")
                    attribute_files = data.get("attribute_files") or []
                    if not isinstance(attribute_files, list) or any(not isinstance(path, str) for path in attribute_files):
                        raise ValueError("Attributes file paths must be a list")
                    absolute_files = [Path(path).expanduser() for path in attribute_files if Path(path).expanduser().is_absolute()]
                    if absolute_files:
                        if network_workspace:
                            raise ValueError("A hosted comparison cannot read an attributes path on the server. Upload the .adoc file instead.")
                        if len(absolute_files) > 1 or len(attribute_files) > 1:
                            raise ValueError("Use one absolute attributes file, or repository-relative files")
                        if data.get("attribute_text", ""):
                            raise ValueError("Choose an attributes file path or upload, not both")
                        attribute_path = absolute_files[0]
                        if attribute_path.suffix.lower() != ".adoc" or not attribute_path.is_file():
                            raise ValueError(f"Attributes file not found or not an .adoc file: {attribute_path}")
                        attribute_text = attribute_path.read_text(encoding="utf-8")
                        attribute_filename = attribute_path.name
                        attribute_files = []
                    else:
                        attribute_text = data.get("attribute_text", "")
                        attribute_filename = data.get("attribute_filename") or None
                    options = dict(repository=data["repository"], base=data["base"], target=data["target"],
                                   patterns=data.get("patterns") or None,
                                   attributes=parse_attributes(data.get("attributes", "").splitlines()),
                                   attribute_files=attribute_files or None, kind=data.get("type", "auto"),
                                   attribute_text=attribute_text,
                                   attribute_filename=attribute_filename,
                                   guide=data.get('guide', '').strip() or None)
                    with jobs_lock:
                        if any(job["status"] == "running" for job in jobs.values()):
                            return self.respond(409, {"error": "A comparison is already running"})
                        while len(jobs) >= 8:
                            jobs.pop(next(iter(jobs)))
                        job_id = secrets.token_hex(12)
                        jobs[job_id] = {"status": "running", "message": "Starting comparison…",
                                        "timeline": ["Starting comparison…"]}
                    def progress(message):
                        with jobs_lock:
                            jobs[job_id]["message"] = message
                            if jobs[job_id]["timeline"][-1] != message:
                                jobs[job_id]["timeline"].append(message)
                    def work():
                        try:
                            with conversion_lock:
                                report = compare(**options, progress=progress)
                            with jobs_lock:
                                timeline = jobs[job_id]["timeline"] + ["Comparison complete. ZIP ready to download."]
                                jobs[job_id] = {"status": "complete", "message": timeline[-1],
                                                "timeline": timeline, "report": report}
                        except Exception as error:
                            with jobs_lock:
                                timeline = jobs[job_id]["timeline"] + ["Comparison failed."]
                                jobs[job_id] = {"status": "error", "message": timeline[-1],
                                                "timeline": timeline, "error": str(error)}
                    threading.Thread(target=work, daemon=True).start()
                    return self.respond(202, {"id": job_id})
                return self.respond(404, {"error": "Not found"})
            except Exception as error:
                return self.respond(400, {"error": str(error)})

    server = ThreadingHTTPServer((bind_host, port), Handler)
    print(f"AsciiDoc → DITA: http://{bind_host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
