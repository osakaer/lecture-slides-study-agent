"""Temporary job storage, cleanup, and pipeline process management.

Jobs stay in memory and their files live only in a temporary workspace.
No database or durable file storage is used; restarting the server clears jobs.
"""

import atexit
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

PROJECT_DIR = Path(__file__).resolve().parent.parent
RESULT_TTL_SECONDS = 29 * 60
JOB_LOCK = threading.Lock()


def process_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def cleanup_stale_server_workspaces() -> None:
    roots = (Path(tempfile.gettempdir()), PROJECT_DIR / "tmp")
    for temp_root in roots:
        for candidate in temp_root.glob("lecture-study-server-*"):
            if not candidate.is_dir():
                continue
            try:
                owner_pid = int((candidate / ".owner-pid").read_text(encoding="ascii"))
            except (OSError, ValueError):
                owner_pid = -1
            if owner_pid == -1:
                try:
                    if time.time() - candidate.stat().st_mtime > 30 * 60:
                        shutil.rmtree(candidate, ignore_errors=True)
                except OSError:
                    pass
            elif not process_is_running(owner_pid):
                shutil.rmtree(candidate, ignore_errors=True)


cleanup_stale_server_workspaces()


def create_server_temp_dir() -> Path:
    try:
        directory = Path(tempfile.mkdtemp(prefix="lecture-study-server-"))
        (directory / ".owner-pid").write_text(str(os.getpid()), encoding="ascii")
        return directory
    except OSError:
        if "directory" in locals():
            shutil.rmtree(directory, ignore_errors=True)
        directory = PROJECT_DIR / "tmp" / f"lecture-study-server-{uuid4().hex}"
        directory.mkdir(parents=True, exist_ok=False)
        (directory / ".owner-pid").write_text(str(os.getpid()), encoding="ascii")
        return directory


SERVER_TEMP_DIR = create_server_temp_dir()

# In-memory job tracker for active pipeline runs
JOBS: dict[str, dict] = {}


def cleanup_job(job: dict) -> None:
    workspace = job.pop("workspace", None)
    if workspace:
        shutil.rmtree(workspace, ignore_errors=True)
    job["files_available"] = False


def cleanup_expired_jobs() -> None:
    now = time.time()
    with JOB_LOCK:
        expired_ids = [
            job_id
            for job_id, job in JOBS.items()
            if job.get("expires_at") and now >= job["expires_at"]
        ]
        expired_jobs = [JOBS.pop(job_id) for job_id in expired_ids]
    for job in expired_jobs:
        cleanup_job(job)


def cleanup_loop() -> None:
    while True:
        time.sleep(15)
        cleanup_expired_jobs()


def cleanup_all_jobs() -> None:
    with JOB_LOCK:
        jobs = list(JOBS.values())
        JOBS.clear()
    for job in jobs:
        cleanup_job(job)
    shutil.rmtree(SERVER_TEMP_DIR, ignore_errors=True)


atexit.register(cleanup_all_jobs)


def get_available_decks(session_id: str) -> list[dict]:
    cleanup_expired_jobs()
    with JOB_LOCK:
        return [
            {"id": job_id, "name": job["deck_name"], "has_pdf": job.get("files_available", False)}
            for job_id, job in JOBS.items()
            if job.get("session_id") == session_id
            and job.get("status") == "completed"
            and job.get("files_available")
        ]


def load_deck_details(job_id: str, session_id: str) -> dict | None:
    job = JOBS.get(job_id)
    if (
        not job
        or job.get("session_id") != session_id
        or job.get("status") != "completed"
        or not job.get("files_available")
    ):
        return None
    deck_dir = Path(job["run_dir"])
    if not deck_dir.exists() or not deck_dir.is_dir():
        return None

    sections_file = deck_dir / "sections.json"
    questions_file = deck_dir / "questions" / "curated_set.json"
    verification_file = deck_dir / "questions" / "verification.json"
    study_guide_file = deck_dir / "study_guide.md"

    sections = []
    if sections_file.exists():
        try:
            raw_sections = json.loads(sections_file.read_text(encoding="utf-8"))
            for sec in raw_sections:
                sec_num = sec["section"]
                summary_file = deck_dir / "section_summaries" / f"section_{sec_num:02}.json"
                summary_data = {}
                if summary_file.exists():
                    summary_data = json.loads(summary_file.read_text(encoding="utf-8"))
                sections.append({
                    "section_number": sec_num,
                    "title": summary_data.get("section_title", f"Section {sec_num}"),
                    "slide_numbers": sec.get("slide_numbers", []),
                    "key_points": summary_data.get("key_points", []),
                })
        except Exception:
            pass

    questions = []
    if questions_file.exists():
        try:
            q_data = json.loads(questions_file.read_text(encoding="utf-8"))
            questions = q_data.get("questions", [])
        except Exception:
            pass

    verifications = {}
    if verification_file.exists():
        try:
            v_data = json.loads(verification_file.read_text(encoding="utf-8"))
            for item in v_data.get("results", []):
                key = (item.get("source_section"), item.get("question"))
                verifications[key] = item.get("verification", {})
        except Exception:
            pass

    # Enrich questions with verification badges
    enriched_questions = []
    for q in questions:
        key = (q.get("source_section"), q.get("question"))
        v_info = verifications.get(key, {})
        enriched_questions.append({
            **q,
            "verification": v_info,
        })

    markdown_guide = ""
    if study_guide_file.exists():
        try:
            markdown_guide = study_guide_file.read_text(encoding="utf-8")
        except Exception:
            pass

    return {
        "name": job_id,
        "display_name": job["deck_name"],
        "sections": sections,
        "questions": enriched_questions,
        "markdown_guide": markdown_guide,
        "has_pdf": (deck_dir / "study_guide.pdf").exists(),
    }


def execute_pipeline(
    job_id: str, pdf_path: Path, questions: int, api_key: str
) -> None:
    job = JOBS[job_id]
    job["status"] = "running"
    job["start_time"] = time.time()

    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    env["GOOGLE_API_KEY"] = api_key
    cmd = [
        sys.executable,
        str(PROJECT_DIR / "src" / "pipeline_worker.py"),
        str(pdf_path),
        str(job["run_dir"]),
        "--questions",
        str(questions),
    ]

    job["logs"].append(f"Starting pipeline on: {pdf_path.name}")
    job["logs"].append(f"Provider: Gemini | Questions: {questions}")

    try:
        process = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            encoding="utf-8",
            errors="replace",
        )
        job["pid"] = process.pid

        current_step = "Initializing"
        for line in process.stdout:
            line_str = line.strip()
            if line_str:
                job["logs"].append(line_str)
                # Keep logs manageable
                if len(job["logs"]) > 500:
                    job["logs"] = job["logs"][-500:]

                # Detect pipeline phase
                if "=== Extract slides" in line_str:
                    current_step = "Extracting & Rendering Slides"
                elif "=== Create slide sections" in line_str:
                    current_step = "Creating Topic Sections"
                elif "=== Summarize section" in line_str:
                    current_step = "Summarizing Sections"
                elif "=== Generate question" in line_str:
                    current_step = "Generating Grounded Questions"
                elif "=== Verify questions" in line_str or "=== Recheck repaired" in line_str:
                    current_step = "Multi-Modal Factual Verification"
                elif "=== Repair unsupported" in line_str:
                    current_step = "Self-Repairing Questions"
                elif "=== Export the study guide" in line_str:
                    current_step = "Compiling Markdown & PDF"

                job["current_step"] = current_step

        process.wait()
        if process.returncode == 0:
            job["status"] = "completed"
            job["current_step"] = "Finished Successfully"
            job["files_available"] = True
            job["expires_at"] = time.time() + RESULT_TTL_SECONDS
            job["logs"].append("Your files are temporary and will be deleted after download or in 30 minutes.")
        else:
            job["status"] = "failed"
            job["error"] = f"Pipeline exited with error code {process.returncode}"
            cleanup_job(job)
            job["expires_at"] = time.time() + RESULT_TTL_SECONDS
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        cleanup_job(job)
        job["expires_at"] = time.time() + RESULT_TTL_SECONDS
    finally:
        env.pop("GOOGLE_API_KEY", None)


