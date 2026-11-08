"""Temporary-upload web UI for the Lecture Slides Study Agent.

Runs a zero-dependency local web server with an interactive web dashboard for:
- Uploading lecture PDFs or selecting previously processed decks
- Running the full multi-modal pipeline with live progress and log streaming
- Exploring section summaries, exam questions, and visual slide verification
- Viewing and downloading generated Markdown and PDF study guides
"""

from email import policy
from email.parser import BytesParser
import io
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import urllib.parse
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

MAX_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_ACTIVE_JOBS = 2

# The random browser cookie scopes temporary jobs without creating user accounts.
from web_jobs import (
    JOBS,
    JOB_LOCK,
    SERVER_TEMP_DIR,
    cleanup_expired_jobs,
    cleanup_job,
    cleanup_loop,
    execute_pipeline,
    get_available_decks,
    load_deck_details,
)

INDEX_HTML = (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")


class StudyAgentHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Keep server quiet unless error
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        params = urllib.parse.parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            if not self._session_id():
                cookie = f"study_session={secrets.token_urlsafe(32)}; HttpOnly; SameSite=Strict; Path=/"
                if self.headers.get("X-Forwarded-Proto", "").lower() == "https":
                    cookie += "; Secure"
                self.send_header(
                    "Set-Cookie",
                    cookie,
                )
            self.end_headers()
            self.wfile.write(INDEX_HTML.encode("utf-8"))
            return

        elif path == "/api/decks":
            session_id = self._session_id()
            if not session_id:
                self._send_api_error(HTTPStatus.UNAUTHORIZED, "Reload the page to start a session")
                return
            decks = get_available_decks(session_id)
            self._send_json(decks)
            return

        elif path == "/api/deck_files":
            self._send_json([])
            return

        elif path == "/api/deck_details":
            name = params.get("name", [""])[0]
            session_id = self._session_id()
            details = load_deck_details(name, session_id or "")
            self._send_json(details)
            return

        elif path == "/api/status":
            job_id = params.get("job_id", [""])[0]
            job = self._owned_job(job_id)
            if job:
                self._send_json({key: job.get(key) for key in (
                    "id", "status", "current_step", "logs", "error", "deck_name",
                    "files_available", "retry_available"
                )})
            else:
                self._send_json({"status": "not_found", "logs": []}, status=HTTPStatus.NOT_FOUND)
            return

        elif path == "/api/download":
            job_id = params.get("job_id", [""])[0]
            job = self._owned_job(job_id)
            if (
                not job
                or job.get("status") != "completed"
                or not job.get("files_available")
            ):
                self.send_error(HTTPStatus.NOT_FOUND, "Guide not found or its temporary download expired")
                return
            run_dir = Path(job["run_dir"])
            pdf_path = run_dir / "study_guide.pdf"
            md_path = run_dir / "study_guide.md"
            if not pdf_path.is_file() or not md_path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND, "Generated guide files are unavailable")
                return
            package = io.BytesIO()
            with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                archive.write(pdf_path, arcname="study_guide.pdf")
                archive.write(md_path, arcname="study_guide.md")
            payload = package.getvalue()
            filename = re.sub(r"[^A-Za-z0-9._-]+", "_", job["deck_name"]).strip("._") or "study_guide"
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Content-Disposition", f'attachment; filename="{filename}_study_guide.zip"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)
            cleanup_job(job)
            job["status"] = "downloaded"
            return

        elif path == "/api/slide_image":
            deck = params.get("deck", [""])[0]
            slide_str = params.get("slide", ["1"])[0]
            try:
                slide_num = int(slide_str)
            except ValueError:
                slide_num = 1

            job = self._owned_job(deck)
            if (
                not job
                or job.get("status") != "completed"
                or not job.get("files_available")
            ):
                self.send_error(HTTPStatus.NOT_FOUND, "Slide image not found")
                return
            img_path = Path(job["run_dir"]) / "rendered" / f"slide_{slide_num:03}.png"
            if img_path.exists():
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "private, no-store")
                self.end_headers()
                self.wfile.write(img_path.read_bytes())
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Slide image not found")
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Endpoint not found")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/api/retry":
            session_id = self._session_id()
            if not session_id:
                self._send_api_error(HTTPStatus.UNAUTHORIZED, "Reload the page to start a session")
                return
            try:
                content_len = int(self.headers.get("Content-Length", 0))
            except ValueError:
                self._send_api_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
                return
            if content_len <= 0 or content_len > 8192:
                self._send_api_error(HTTPStatus.BAD_REQUEST, "Invalid retry request")
                return
            try:
                fields = urllib.parse.parse_qs(
                    self.rfile.read(content_len).decode("utf-8"),
                    keep_blank_values=True,
                )
            except UnicodeError:
                self._send_api_error(HTTPStatus.BAD_REQUEST, "Could not read the retry request")
                return

            job_id = fields.get("job_id", [""])[0]
            api_key = fields.get("api_key", [""])[0].strip()
            job = self._owned_job(job_id)
            if not job or job.get("status") != "failed" or not job.get("retry_available"):
                self._send_api_error(HTTPStatus.NOT_FOUND, "This failed job is no longer available to retry")
                return
            if not api_key:
                self._send_api_error(HTTPStatus.BAD_REQUEST, "Enter your Google AI API key to retry")
                return
            pdf_path = Path(job.get("pdf_path", ""))
            run_dir = Path(job.get("run_dir", ""))
            if not pdf_path.is_file() or not run_dir.is_dir():
                cleanup_job(job)
                self._send_api_error(HTTPStatus.GONE, "Temporary progress expired. Upload the PDF again.")
                return

            with JOB_LOCK:
                active_jobs = sum(
                    item.get("status") in {"pending", "running"}
                    for item in JOBS.values()
                )
                if active_jobs >= MAX_ACTIVE_JOBS:
                    self._send_api_error(HTTPStatus.SERVICE_UNAVAILABLE, "The server is busy. Try again shortly.")
                    return
                job["status"] = "pending"
                job["current_step"] = "Retry queued"
                job["retry_available"] = False
                job.pop("expires_at", None)
                job["retry_count"] = job.get("retry_count", 0) + 1
                job["logs"].append(
                    "Retrying from temporary progress; completed Gemini responses will be reused where available."
                )
                thread = threading.Thread(
                    target=execute_pipeline,
                    args=(job_id, pdf_path, job["question_count"], api_key),
                    daemon=True,
                )
                try:
                    thread.start()
                except RuntimeError:
                    job["status"] = "failed"
                    job["retry_available"] = True
                    self._send_api_error(HTTPStatus.SERVICE_UNAVAILABLE, "Could not start the retry")
                    return
            self._send_json({"job_id": job_id})
            return

        if path == "/api/run":
            session_id = self._session_id()
            if not session_id:
                self._send_api_error(HTTPStatus.UNAUTHORIZED, "Reload the page to start a session")
                return
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("multipart/form-data"):
                self._send_api_error(HTTPStatus.BAD_REQUEST, "Expected multipart/form-data")
                return

            try:
                content_len = int(self.headers.get("Content-Length", 0))
            except ValueError:
                self._send_api_error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
                return
            if content_len <= 0 or content_len > MAX_UPLOAD_BYTES + 1024 * 1024:
                self._send_api_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "PDF upload limit is 50 MB")
                return

            body = self.rfile.read(content_len)
            try:
                message = BytesParser(policy=policy.default).parsebytes(
                    f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii")
                    + body
                )
                fields = {}
                uploaded_file = None
                uploaded_filename = None
                for part in message.iter_parts():
                    field_name = part.get_param("name", header="content-disposition")
                    payload = part.get_payload(decode=True) or b""
                    filename = part.get_filename()
                    if field_name == "pdf" and filename:
                        uploaded_filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
                        uploaded_file = payload
                    elif field_name:
                        fields[field_name] = payload.decode(
                            part.get_content_charset() or "utf-8", errors="replace"
                        ).strip()
            except (UnicodeError, ValueError, TypeError):
                self._send_api_error(HTTPStatus.BAD_REQUEST, "Could not read the uploaded form")
                return

            try:
                questions = int(fields.get("questions", 5))
            except ValueError:
                self._send_api_error(HTTPStatus.BAD_REQUEST, "Question count must be a number")
                return
            if questions not in {5, 10, 12, 15}:
                self._send_api_error(HTTPStatus.BAD_REQUEST, "Question count must be 5, 10, 12, or 15")
                return
            api_key = fields.get("api_key", "")
            if not api_key:
                self._send_api_error(HTTPStatus.BAD_REQUEST, "A Google AI API key is required for Gemini")
                return
            if not uploaded_file or not uploaded_filename or Path(uploaded_filename).suffix.lower() != ".pdf":
                self._send_api_error(HTTPStatus.BAD_REQUEST, "Choose a PDF file to upload")
                return
            if len(uploaded_file) > MAX_UPLOAD_BYTES:
                self._send_api_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "PDF upload limit is 50 MB")
                return
            if not uploaded_file.startswith(b"%PDF-"):
                self._send_api_error(HTTPStatus.BAD_REQUEST, "The uploaded file does not appear to be a PDF")
                return

            job_id = str(uuid4())
            workspace = Path(tempfile.mkdtemp(prefix="job-", dir=SERVER_TEMP_DIR))
            target_pdf_path = workspace / Path(uploaded_filename).name
            run_dir = workspace / "run"
            try:
                target_pdf_path.write_bytes(uploaded_file)
            except OSError:
                shutil.rmtree(workspace, ignore_errors=True)
                self._send_api_error(HTTPStatus.INSUFFICIENT_STORAGE, "Could not temporarily store the uploaded PDF")
                return
            if not target_pdf_path.is_file():
                self._send_api_error(HTTPStatus.BAD_REQUEST, "No valid PDF file provided")
                shutil.rmtree(workspace, ignore_errors=True)
                return

            job_record = {
                "id": job_id,
                # Every job endpoint checks this owner before exposing its files or status.
                "session_id": session_id,
                "status": "pending",
                "current_step": "Queued",
                "logs": [],
                "deck_name": target_pdf_path.stem,
                "workspace": str(workspace),
                "run_dir": str(run_dir),
                "pdf_path": str(target_pdf_path),
                "question_count": questions,
                "files_available": False,
                "retry_available": False,
                "retry_count": 0,
            }
            with JOB_LOCK:
                active_jobs = sum(job.get("status") in {"pending", "running"} for job in JOBS.values())
                if active_jobs >= MAX_ACTIVE_JOBS:
                    shutil.rmtree(workspace, ignore_errors=True)
                    self._send_api_error(HTTPStatus.SERVICE_UNAVAILABLE, "The server is busy. Try again shortly.")
                    return
                JOBS[job_id] = job_record
                thread = threading.Thread(
                    target=execute_pipeline,
                    args=(job_id, target_pdf_path, questions, api_key),
                    daemon=True,
                )
                try:
                    thread.start()
                except RuntimeError:
                    JOBS.pop(job_id, None)
                    shutil.rmtree(workspace, ignore_errors=True)
                    self._send_api_error(HTTPStatus.SERVICE_UNAVAILABLE, "Could not start a processing job")
                    return

            self._send_json({"job_id": job_id})
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Endpoint not found")

    def _send_json(self, data, status=HTTPStatus.OK):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_api_error(self, status, message):
        self._send_json({"error": message}, status=status)

    def _session_id(self) -> str | None:
        cookie_header = self.headers.get("Cookie", "")
        for cookie in cookie_header.split(";"):
            name, separator, value = cookie.strip().partition("=")
            if separator and name == "study_session" and value:
                return value
        return None

    def _owned_job(self, job_id: str) -> dict | None:
        cleanup_expired_jobs()
        session_id = self._session_id()
        job = JOBS.get(job_id)
        if session_id and job and job.get("session_id") == session_id:
            return job
        return None


def main():
    port = int(os.environ.get("PORT", 8000))
    host = os.environ.get("HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), StudyAgentHandler)
    threading.Thread(target=cleanup_loop, daemon=True).start()
    print(f"\n=======================================================")
    print("Lecture Slides Study Agent - Web Dashboard Ready!")
    print(f"Local Access: http://{host}:{port}")
    print(f"=======================================================\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.server_close()


if __name__ == "__main__":
    main()
