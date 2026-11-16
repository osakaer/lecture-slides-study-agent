"""Streamlit interface for the local lecture study-guide pipeline."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import streamlit as st

PROJECT_DIR = Path(__file__).resolve().parent.parent
WORKER = PROJECT_DIR / "src" / "pipeline_worker.py"
MAX_UPLOAD_BYTES = 50 * 1024 * 1024
QUESTION_OPTIONS = (5, 10, 12, 15)

st.set_page_config(page_title="Lecture Study Guide", layout="wide")


def make_downloads(run_dir: Path) -> tuple[bytes, bytes, bytes]:
    """Load outputs into memory, so the temporary workspace can be deleted."""
    guide_pdf = (run_dir / "study_guide.pdf").read_bytes()
    guide_md = (run_dir / "study_guide.md").read_bytes()
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("study_guide.pdf", guide_pdf)
        archive.writestr("study_guide.md", guide_md)
    return guide_pdf, guide_md, archive_buffer.getvalue()


def load_result(run_dir: Path, filename: str) -> dict:
    return json.loads((run_dir / filename).read_text(encoding="utf-8"))


def run_pipeline(uploaded_pdf, question_count: int) -> None:
    """Run the existing pipeline and retain only display/download data in session memory."""
    try:
        sys.path.insert(0, str(PROJECT_DIR / "src"))
        from model_provider import ensure_model_ready

        ensure_model_ready()
    except Exception as error:
        st.error(str(error))
        return

    progress = st.status("Preparing your study guide", expanded=True)
    log_view = st.empty()
    log_lines: list[str] = []
    try:
        with tempfile.TemporaryDirectory(prefix="lecture-study-") as temp_name:
            temp_dir = Path(temp_name)
            pdf_path = temp_dir / Path(uploaded_pdf.name).name
            pdf_path.write_bytes(uploaded_pdf.getvalue())
            run_dir = temp_dir / "run"
            command = [
                sys.executable,
                "-u",
                str(WORKER),
                str(pdf_path),
                str(run_dir),
                "--questions",
                str(question_count),
            ]
            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            process = subprocess.Popen(
                command,
                cwd=PROJECT_DIR,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip()
                if line:
                    log_lines.append(line)
                    if line.startswith("=== "):
                        progress.update(label=line.strip("= "))
                    if (
                        line.startswith("=== ")
                        or line.startswith("  Local model is still generating")
                        or len(log_lines) % 8 == 0
                    ):
                        log_view.code("\n".join(log_lines[-24:]), language="text")
            return_code = process.wait()
            if return_code != 0:
                progress.update(label="Guide creation failed", state="error")
                st.error("The pipeline stopped. Check the output above for the failing step.")
                return

            slides = load_result(run_dir, "extracted.json")
            sections = load_result(run_dir, "sections.json")
            summaries = {
                path.stem: json.loads(path.read_text(encoding="utf-8"))
                for path in sorted((run_dir / "section_summaries").glob("section_*.json"))
            }
            questions = load_result(run_dir, "questions/curated_set.json")
            verification = load_result(run_dir, "questions/verification.json")
            repair_log_path = run_dir / "questions/repair_log.json"
            repair_log = (
                json.loads(repair_log_path.read_text(encoding="utf-8"))
                if repair_log_path.exists()
                else {"repairs": []}
            )
            guide_pdf, guide_md, guide_zip = make_downloads(run_dir)
            cited_slides = {item["source_slide"] for item in questions.get("questions", [])}
            slide_images = {}
            for slide in slides:
                if slide["slide"] not in cited_slides:
                    continue
                image_path = Path(slide["image"])
                if image_path.is_file():
                    slide_images[slide["slide"]] = image_path.read_bytes()

            st.session_state["study_result"] = {
                "filename": uploaded_pdf.name,
                "slides": slides,
                "sections": sections,
                "summaries": summaries,
                "questions": questions,
                "verification": verification,
                "repair_log": repair_log,
                "guide_pdf": guide_pdf,
                "guide_md": guide_md,
                "guide_zip": guide_zip,
                "slide_images": slide_images,
            }
        progress.update(label="Study guide ready", state="complete", expanded=False)
    except Exception as error:
        progress.update(label="Guide creation failed", state="error")
        st.exception(error)


def show_result(result: dict) -> None:
    questions = result["questions"].get("questions", [])
    verification = result["verification"]
    results_by_question = {
        (item.get("source_section"), item["question"]): item["verification"]
        for item in verification.get("results", [])
    }
    repair_indices = {
        item["question_index"]
        for item in result["repair_log"].get("repairs", [])
        if item.get("status") == "repaired"
    }

    st.subheader(result["filename"])
    metric_columns = st.columns(3)
    metric_columns[0].metric("Slides", len(result["slides"]))
    metric_columns[1].metric("Sections", len(result["sections"]))
    metric_columns[2].metric("Practice questions", len(questions))
    downloads = st.columns(2)
    downloads[0].download_button(
        "Download study guide PDF",
        result["guide_pdf"],
        file_name="study_guide.pdf",
        mime="application/pdf",
        use_container_width=True,
    )
    downloads[1].download_button(
        "Download PDF + Markdown ZIP",
        result["guide_zip"],
        file_name="study_guide.zip",
        mime="application/zip",
        use_container_width=True,
    )

    summary_tab, question_tab, guide_tab = st.tabs(
        ["Section summaries", "Practice questions", "Printable guide"]
    )
    with summary_tab:
        for section in result["sections"]:
            number = section["section"]
            summary = result["summaries"].get(f"section_{number:02}")
            if not summary:
                continue
            with st.expander(f"Section {number}: {summary['section_title']}"):
                for point in summary["key_points"]:
                    citations = ", ".join(map(str, point["source_slides"]))
                    st.markdown(f"- {point['text']}  \n*Slides {citations}*")

    with question_tab:
        for index, question in enumerate(questions, start=1):
            check = results_by_question.get(
                (question.get("source_section"), question["question"]), {}
            )
            status = check.get("supported", "not checked")
            badge = " · Repaired" if index in repair_indices else ""
            with st.expander(f"Question {index} · {status.title()}{badge}"):
                st.markdown(f"**{question['question']}**")
                st.markdown(f"**Answer:** {question['answer']}")
                st.caption(
                    f"Type: {question['type']} · Source slide: {question['source_slide']}"
                )
                if check:
                    st.write(check.get("explanation", ""))
                    for claim in check.get("claims", []):
                        st.markdown(
                            f"- **{claim['source'].title()} claim — {claim['status']}**: "
                            f"{claim['claim']}  \nEvidence: {claim['evidence']}"
                        )
                image = result["slide_images"].get(question["source_slide"])
                if image:
                    st.image(image, caption=f"Cited slide {question['source_slide']}")

    with guide_tab:
        st.markdown(result["guide_md"].decode("utf-8"))


def main() -> None:
    st.title("Lecture Slides Study Guide")
    st.write("Turn a lecture PDF into cited summaries, checked practice questions, and a printable guide.")
    st.info(
        "Runs locally with Ollama. Your PDF and intermediate files are kept in a temporary "
        "folder and removed when processing ends."
    )

    if st.session_state.get("study_result"):
        result = st.session_state["study_result"]
        if st.button("Start another guide"):
            del st.session_state["study_result"]
            st.rerun()
        show_result(result)
        return

    with st.form("study_guide_form"):
        uploaded_pdf = st.file_uploader(
            "Choose a lecture PDF",
            type=["pdf"],
            help="Maximum file size: 50 MB.",
        )
        question_count = st.select_slider(
            "Number of practice questions",
            options=QUESTION_OPTIONS,
            value=5,
        )
        submitted = st.form_submit_button("Create my study guide", type="primary")

    if submitted:
        if uploaded_pdf is None:
            st.error("Choose a PDF before creating a guide.")
        elif uploaded_pdf.size > MAX_UPLOAD_BYTES:
            st.error("This PDF is larger than the 50 MB limit.")
        elif uploaded_pdf.getvalue()[:5] != b"%PDF-":
            st.error("The selected file does not appear to be a valid PDF.")
        else:
            run_pipeline(uploaded_pdf, question_count)
            result = st.session_state.get("study_result")
            if result:
                show_result(result)


if __name__ == "__main__":
    main()
