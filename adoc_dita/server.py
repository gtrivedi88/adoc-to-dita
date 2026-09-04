"""Loopback-only interface. No account, database, upload service or external assets."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit, parse_qs

from .cli import parse_attributes
from .converter import convert_text
from .context import convert_in_repository, infer_repository, SourceNotFoundError
from .repository import compare, refs
from .report import zip_report


def serve(port=8765):
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
            host = self.headers.get("Host", "")
            expected = {f"127.0.0.1:{port}", f"localhost:{port}"}
            origin = self.headers.get("Origin")
            if host not in expected or (origin and origin not in {"http://" + x for x in expected}):
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
                    return self.respond(200, {"refs": refs(data["repository"])})
                if self.path == "/api/convert":
                    with conversion_lock:
                        options = dict(filename=data.get("filename", "document.adoc"),
                                       attributes=parse_attributes(data.get("attributes", "").splitlines()), kind=data.get("type", "auto"))
                        attribute_file = data.get("attribute_file", "").strip() or None
                        repository = data.get('local_repository', '').strip() or infer_repository(attribute_file)
                        if repository:
                            try:
                                result = convert_in_repository(data['source'], repository=repository, **options,
                                                               source_path=data.get('source_path') or None,
                                                               guide=data.get('guide') or None, profile=data.get('profile') or None,
                                                               attribute_file=attribute_file)
                            except SourceNotFoundError:
                                if data.get('local_repository', '').strip() or data.get('source_path') or data.get('guide'):
                                    raise
                                result = convert_text(data['source'], **options, attribute_file=attribute_file)
                        else:
                            result = convert_text(data['source'], **options, attribute_file=attribute_file)
                    return self.respond(200, result)
                if self.path == "/api/compare":
                    options = dict(repository=data["repository"], base=data["base"], target=data["target"],
                                   patterns=data.get("patterns") or None,
                                   attributes=parse_attributes(data.get("attributes", "").splitlines()),
                                   attribute_files=data.get("attribute_files"), kind=data.get("type", "auto"),
                                   guide=data.get('guide', '').strip() or None)
                    with jobs_lock:
                        if any(job["status"] == "running" for job in jobs.values()):
                            return self.respond(409, {"error": "A comparison is already running"})
                        while len(jobs) >= 8:
                            jobs.pop(next(iter(jobs)))
                        job_id = secrets.token_hex(12)
                        jobs[job_id] = {"status": "running", "message": "Starting comparison…"}
                    def progress(message):
                        with jobs_lock:
                            jobs[job_id]["message"] = message
                    def work():
                        try:
                            with conversion_lock:
                                report = compare(**options, progress=progress)
                            with jobs_lock:
                                jobs[job_id] = {"status": "complete", "report": report}
                        except Exception as error:
                            with jobs_lock:
                                jobs[job_id] = {"status": "error", "error": str(error)}
                    threading.Thread(target=work, daemon=True).start()
                    return self.respond(202, {"id": job_id})
                return self.respond(404, {"error": "Not found"})
            except Exception as error:
                return self.respond(400, {"error": str(error)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"AsciiDoc → DITA: http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
