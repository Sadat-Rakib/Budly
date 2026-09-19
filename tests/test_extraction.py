"""Document extraction and the guards on what the model returns.

The guards matter more than the extraction: a syllabus parser that is occasionally wrong
is fine, one that confidently invents an exam date is not.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from canvasbuddy.agent.extraction import (
    _coerce_date,
    _coerce_time,
    _in_term,
    _normalise,
    _parse_payload,
)
from canvasbuddy.documents.extract import (
    SYLLABUS_SCORE_FLOOR,
    ExtractionError,
    content_score,
    extract_text,
    looks_like_syllabus,
    sniff,
)
from canvasbuddy.models import Course

# Trimmed from the genuine MGHB02 syllabus.
SYLLABUS = """
MGHB02H3F Managing People and Groups in Organizations

Grading scheme
Self-Reflection Assignment | 10% | This assignment uses the textbook | 2026-11
Mid-Term Test | 35% | The midterm exam will be an in-person proctored exam
Final Exam | 35% | The final exam covers course material from the midterm onwards
Participation | 20% | Weekly quizzes

Late policy: assignments submitted late lose 10% per day.
Academic integrity is taken seriously in this course.
"""

# Trimmed from the WileyPLUS card that sits in MGAB03 named like a syllabus.
NOT_A_SYLLABUS = """
How to access your course
Log in to WileyPLUS. Find your course. Register and access.
Go to www.wileyplus.com/go/wpngsupport for registration help.
Enter your Course Section ID and select Find my course.
"""


class TestSniff:
    @pytest.mark.parametrize(
        ("magic", "expected"),
        [
            (b"PK\x03\x04rest", "docx"),
            (b"%PDF-1.7", "pdf"),
            (b"\xd0\xcf\x11\xe0\xa1\xb1", "doc"),
            (b"\x89PNG\r\n", "unknown"),
            (b"", "unknown"),
        ],
    )
    def test_format_is_read_from_magic_bytes(self, magic: bytes, expected: str) -> None:
        """Dispatch on content, not extension: Canvas content types are often wrong."""
        assert sniff(magic) == expected

    def test_unsupported_format_raises(self) -> None:
        with pytest.raises(ExtractionError):
            extract_text(b"\x89PNG\r\n\x1a\n")


class TestContentScore:
    def test_a_real_syllabus_scores_well(self) -> None:
        assert content_score(SYLLABUS) >= SYLLABUS_SCORE_FLOOR
        assert looks_like_syllabus(SYLLABUS)

    def test_the_wileyplus_card_scores_zero(self) -> None:
        """The exact false positive this exists to stop.

        The real file is named "MGAB03 L04 - Kong - Fall 2026.pdf" and reads like a
        section syllabus. Any filename heuristic sends it to the model, which then has
        nothing to work from and invents dates.
        """
        assert content_score(NOT_A_SYLLABUS) == 0
        assert not looks_like_syllabus(NOT_A_SYLLABUS)

    def test_the_syllabus_outranks_the_card(self) -> None:
        assert content_score(SYLLABUS) > content_score(NOT_A_SYLLABUS)

    def test_empty_text_scores_zero(self) -> None:
        assert content_score("") == 0


class TestPayloadParsing:
    def test_plain_json(self) -> None:
        assert _parse_payload('{"assessments": []}') == {"assessments": []}

    def test_json_wrapped_in_a_code_fence(self) -> None:
        raw = '```json\n{"assessments": [{"title": "Final"}]}\n```'
        assert _parse_payload(raw)["assessments"][0]["title"] == "Final"

    def test_json_with_a_preamble(self) -> None:
        raw = 'Here is what I found:\n{"assessments": []}'
        assert _parse_payload(raw) == {"assessments": []}

    def test_no_json_at_all_raises(self) -> None:
        from canvasbuddy.llm.openrouter import LLMError

        with pytest.raises(LLMError):
            _parse_payload("I could not find any assessments.")


class TestCoercion:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("2026-10-15", date(2026, 10, 15)), ("2026-10-15T00:00", date(2026, 10, 15))],
    )
    def test_dates(self, raw: str, expected: date) -> None:
        assert _coerce_date(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "TBD", "sometime in October", 42])
    def test_unparseable_dates_become_none(self, raw: object) -> None:
        """ "Date TBD" must become null, never an invented date."""
        assert _coerce_date(raw) is None

    @pytest.mark.parametrize(
        ("raw", "hour", "minute"), [("19:00", 19, 0), ("7:00PM", 19, 0), ("9AM", 9, 0)]
    )
    def test_times(self, raw: str, hour: int, minute: int) -> None:
        parsed = _coerce_time(raw)
        assert parsed is not None
        assert (parsed.hour, parsed.minute) == (hour, minute)

    def test_unparseable_time_is_none(self) -> None:
        assert _coerce_time("evening") is None


class TestTermWindow:
    def course(self) -> Course:
        c = Course(canvas_id=1, code="MGAB03", name="x")
        c.term_start_at = datetime.fromisoformat("2026-05-04T04:00:00+00:00")
        c.term_end_at = datetime.fromisoformat("2027-01-31T05:00:00+00:00")
        return c

    def test_a_date_inside_the_term_is_kept(self) -> None:
        assert _in_term(date(2026, 10, 15), self.course())

    def test_a_date_before_the_term_is_rejected(self) -> None:
        """A 2025 date from a reused shell, or a hallucination. Either way, not real."""
        assert not _in_term(date(2025, 10, 15), self.course())

    def test_a_date_after_the_term_is_rejected(self) -> None:
        assert not _in_term(date(2027, 6, 1), self.course())

    def test_no_date_is_allowed(self) -> None:
        """ "Final Exam (35%) Date TBD" is real information worth keeping."""
        assert _in_term(None, self.course())

    def test_a_course_with_no_term_bounds_accepts_anything(self) -> None:
        c = Course(canvas_id=1, code="X", name="x")
        c.term_start_at = c.term_end_at = None
        assert _in_term(date(2030, 1, 1), c)


class TestSourceQuoteGuard:
    """The quote check is what makes a wrong answer auditable rather than plausible."""

    def test_a_real_quote_is_found(self) -> None:
        quote = "The final exam covers course material from the midterm onwards"
        assert _normalise(quote) in _normalise(SYLLABUS)

    def test_an_invented_quote_is_not_found(self) -> None:
        quote = "The midterm will be held on October 15 at 7pm in IC 130"
        assert _normalise(quote) not in _normalise(SYLLABUS)

    def test_line_wrapping_does_not_defeat_the_check(self) -> None:
        """Extracted text wraps unpredictably; the check normalises whitespace first."""
        wrapped = "The  final exam\ncovers   course material\nfrom the midterm onwards"
        assert _normalise(wrapped) in _normalise(SYLLABUS)

    def test_a_paraphrase_is_rejected(self) -> None:
        assert _normalise("The final covers material after the midterm") not in _normalise(SYLLABUS)


class TestModuleTraversal:
    def test_only_file_items_with_urls_are_collected(self) -> None:
        from canvasbuddy.documents.fetch import _module_file_items

        modules = [
            {
                "name": "Course Outline",
                "items": [
                    {"type": "File", "title": "syllabus.pdf", "url": "https://x/1"},
                    {"type": "Page", "title": "Welcome", "url": "https://x/2"},
                    {"type": "File", "title": "broken", "url": None},
                    {"type": "ExternalUrl", "title": "Zoom", "url": "https://x/3"},
                ],
            },
            {"name": "Empty", "items": []},
            {"name": "No items key"},
        ]
        items = _module_file_items(modules)
        assert [i["title"] for i in items] == ["syllabus.pdf"]


class TestExamButtonCallback:
    def test_callback_data_round_trips(self) -> None:
        """Telegram caps callback_data at 64 bytes; this stays far inside it."""
        payload = "exam:ok:12345"
        assert len(payload.encode()) < 64
        prefix, action, raw_id = payload.split(":")
        assert (prefix, action, int(raw_id)) == ("exam", "ok", 12345)


class TestReportSummaries:
    def test_ingest_summary_lists_candidates_best_first(self) -> None:
        from canvasbuddy.documents.fetch import IngestReport

        report = IngestReport(downloaded=3)
        report.candidates = [("MGAB03", "outline.doc", 33), ("MGHB02", "syllabus.docx", 50)]
        summary = report.summary()
        assert summary.index("syllabus.docx") < summary.index("outline.doc")

    def test_extraction_summary_flags_unconfirmed(self) -> None:
        from canvasbuddy.agent.extraction import ExtractionResult
        from canvasbuddy.models import Exam, ExamKind, ExamSource

        exam = Exam(
            course_id=1,
            title="Mid-Term Test",
            kind=ExamKind.midterm,
            date=date(2026, 10, 15),
            source_type=ExamSource.syllabus_pdf,
            confirmed_by_user=False,
        )
        result = ExtractionResult(course_code="MGHB02", created=[exam])
        assert "needs confirming" in result.summary()


def test_extraction_prompt_forbids_invented_dates() -> None:
    """A regression guard on the instruction that stops "Date TBD" becoming a date."""
    from canvasbuddy.agent.extraction import _SYSTEM

    assert "Never guess a date" in _SYSTEM
    assert "VERBATIM" in _SYSTEM
    assert json.loads('{"assessments": []}') == {"assessments": []}
