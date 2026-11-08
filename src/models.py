"""Domain models and schemas for the Lecture Slides Study Agent.

Centralizes all Pydantic models for slides, section chunking, summaries,
questions, factual verification, and repair pipelines.
"""

from typing import Literal
from pydantic import BaseModel, Field

# ── Slide & Section Types ──────────────────────────────────────────────────

class Slide(BaseModel):
    """Extracted text and visual rendering metadata for a single slide."""
    slide: int
    text: str
    image: str


class Section(BaseModel):
    """A logical grouping of consecutive slides."""
    section: int
    slide_numbers: list[int]


class KeyPoint(BaseModel):
    """A single summarized takeaway with cited source slide numbers."""
    text: str
    source_slides: list[int]


class SectionSummary(BaseModel):
    """Complete summary of a section with title and key takeaways."""
    section_title: str
    key_points: list[KeyPoint]


class BatchedSectionSummary(SectionSummary):
    """A section summary tagged with its section index in a batch."""
    section_number: int = Field(ge=1)


class SectionSummaryBatch(BaseModel):
    """Multiple section summaries returned in one model response."""
    sections: list[BatchedSectionSummary] = Field(min_length=1)


# ── Question Generation Types ──────────────────────────────────────────────

QuestionType = Literal["definition", "why/how", "compare", "application"]


class ExamQuestion(BaseModel):
    """An exam-style question candidate generated from a cited slide."""
    question: str
    answer: str
    type: QuestionType
    source_slide: int


class BatchedExamQuestion(ExamQuestion):
    """A generated question tagged with its position and source section."""
    question_number: int = Field(ge=1)
    source_section: int = Field(ge=1)
    candidate_number: int = Field(ge=1)


class ExamQuestionBatch(BaseModel):
    """A section's questions returned together in one model response."""
    questions: list[BatchedExamQuestion] = Field(min_length=1)


class CuratedQuestion(BaseModel):
    """An exam question enriched with section and candidate tracking."""
    source_section: int | None = None
    candidate_number: int = 1
    question: str
    answer: str
    type: QuestionType
    source_slide: int
    include_slide_image: bool = True
    verification_hint: str | None = None


class CuratedQuestionSet(BaseModel):
    """A collection of curated questions ready for verification."""
    title: str
    question_count: int
    questions: list[CuratedQuestion]


# ── Verification Types ─────────────────────────────────────────────────────

ClaimStatus = Literal["supported", "unsupported", "contradicted", "unclear"]
ClaimSource = Literal["question", "answer"]
OverallStatus = Literal["yes", "partial", "no"]


class ClaimAssessment(BaseModel):
    """Factual check for an individual claim against a cited slide."""
    source: ClaimSource
    claim: str
    status: ClaimStatus
    evidence: str


class ClaimReview(BaseModel):
    """Model output payload containing all extracted claims and an explanation."""
    claims: list[ClaimAssessment] = Field(min_length=1)
    explanation: str


class BatchedClaimReview(BaseModel):
    """Verification outcome for one question within a verification batch."""
    question_number: int = Field(ge=1)
    claims: list[ClaimAssessment] = Field(min_length=1)
    explanation: str


class ClaimReviewBatch(BaseModel):
    """One verifier response containing outcomes for multiple questions."""
    results: list[BatchedClaimReview] = Field(min_length=1)


class VerificationResult(BaseModel):
    """Aggregated verification verdict for a question."""
    supported: OverallStatus
    explanation: str
    claims: list[ClaimAssessment]


class QuestionVerification(BaseModel):
    """A question paired with its factual verification outcome."""
    source_section: int | None = None
    question: str
    answer: str
    type: QuestionType
    source_slide: int
    input_fingerprint: str | None = None
    verification: VerificationResult


class VerificationSet(BaseModel):
    """Complete set of verification results for a lecture run."""
    verifier: dict = Field(default_factory=dict)
    counts: dict[str, int] = Field(default_factory=dict)
    results: list[QuestionVerification] = Field(default_factory=list)


# ── Repair Types ───────────────────────────────────────────────────────────

RepairStatus = Literal["repaired", "unchanged", "failed"]


class RepairItem(BaseModel):
    """Audit record of an automated question repair attempt."""
    question_index: int
    original_question: str
    repaired_question: str
    original_answer: str
    repaired_answer: str
    source_slide: int
    status: RepairStatus
    reason: str | None = None


class RepairLog(BaseModel):
    """Log of all repairs performed on a question set."""
    repairs: list[RepairItem] = Field(default_factory=list)


# ── Job Types ──────────────────────────────────────────────────────────────

JobStatus = Literal["pending", "running", "completed", "failed"]
