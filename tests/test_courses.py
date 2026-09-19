"""Term filtering and section matching, against real Canvas strings."""

from __future__ import annotations

import pytest

from canvasbuddy.sync.courses import (
    SectionRef,
    course_title,
    enrolled_section_refs,
    is_tracked_term,
    name_stem,
    parse_section_hint,
    parse_section_name,
    short_course_code,
)


class TestTermFilter:
    def test_matching_term_is_tracked(self) -> None:
        assert is_tracked_term("2026 Fall", "2026 Fall")

    def test_case_and_padding_are_ignored(self) -> None:
        assert is_tracked_term("  2026 fall ", "2026 Fall")

    def test_other_terms_are_excluded(self) -> None:
        """Summer enrolments stay 'active' well into the fall."""
        assert not is_tracked_term("2026 Summer", "2026 Fall")

    def test_default_term_is_never_tracked(self) -> None:
        """Canvas' catch-all holds orientation modules, residence, and club shells."""
        assert not is_tracked_term("Default Term", "Default Term")
        assert not is_tracked_term("Default Term", "2026 Fall")

    def test_missing_term_is_not_tracked(self) -> None:
        assert not is_tracked_term(None, "2026 Fall")


class TestSectionHints:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("Group Project - L01", SectionRef("LEC", 1)),
            ("Group Project - L04", SectionRef("LEC", 4)),
            ("Midterm (LEC 02)", SectionRef("LEC", 2)),
            ("Essay - Section 3", SectionRef("LEC", 3)),
            ("Lab report - TUT0002", SectionRef("TUT", 2)),
            ("Presentation - PRA01", SectionRef("PRA", 1)),
        ],
    )
    def test_recognised_suffixes(self, name: str, expected: SectionRef) -> None:
        assert parse_section_hint(name) == expected

    @pytest.mark.parametrize(
        "name",
        [
            "Reading response #1",
            "Ch. 1 Quiz: Organizational Behaviour and Management",
            "Participation",
            "Midterm Score",
            "Accounting Orientation Day",
            # A trailing number that is not a section is the important false positive
            # to avoid: this is the tenth reading response, not section 10.
            "Reading response 10",
        ],
    )
    def test_names_without_a_section_return_none(self, name: str) -> None:
        assert parse_section_hint(name) is None

    def test_section_names_parse(self) -> None:
        assert parse_section_name("MGAB03H3-F-LEC04-20269") == SectionRef("LEC", 4)
        assert parse_section_name("MGAB03H3-F-TUT0002-20269") == SectionRef("TUT", 2)

    def test_hint_and_section_name_agree(self) -> None:
        """The whole point: 'L04' in a name and 'LEC04' in a section are one section."""
        assert parse_section_hint("Group Project - L04") == parse_section_name(
            "MGAB03H3-F-LEC04-20269"
        )

    def test_enrolled_refs_from_live_data(self) -> None:
        refs = enrolled_section_refs(["MGAB03H3-F-LEC04-20269", "MGAB03H3-F-TUT0002-20269"])
        assert refs == {SectionRef("LEC", 4), SectionRef("TUT", 2)}

    def test_enrolled_refs_tolerates_nothing(self) -> None:
        assert enrolled_section_refs(None) == set()


class TestNameStem:
    def test_variants_share_a_stem(self) -> None:
        stems = {name_stem(f"Group Project - L0{i}") for i in range(1, 5)}
        assert stems == {"Group Project"}

    def test_plain_names_are_unchanged(self) -> None:
        assert name_stem("Reading response #1") == "Reading response #1"


class TestCourseNaming:
    @pytest.mark.parametrize(
        ("code", "expected"),
        [
            ("MGAB03H3 F LEC01 20269", "MGAB03"),
            ("MGAB03H3", "MGAB03"),
            ("PHLB18H3 F LEC01 20269", "PHLB18"),
            ("MGHB02H3 F 20269", "MGHB02"),
            ("FSTA02H3 F LEC01 20265", "FSTA02"),
            # Not a UofT-style code: returned untouched rather than mangled, so this
            # stays safe for other Canvas institutions.
            ("SVEP-BCC", "SVEP-BCC"),
            ("UTSC-Living-in-Residence", "UTSC-Living-in-Residence"),
            ("", ""),
        ],
    )
    def test_short_code(self, code: str, expected: str) -> None:
        assert short_course_code(code) == expected

    @pytest.mark.parametrize(
        ("name", "code", "expected"),
        [
            (
                "MGAB03H3 F LEC01 20269:Introductory Management Accounting",
                "MGAB03H3 F LEC01 20269",
                "Introductory Management Accounting",
            ),
            # A title containing its own colon must survive: split on the first only.
            (
                "MGEB02H3 F 20269:Price Theory: A Mathematical Approach",
                "MGEB02H3 F 20269",
                "Price Theory: A Mathematical Approach",
            ),
            (
                "PHLB18H3 F LEC01 20269:Ethics of Artificial Intelligence",
                "PHLB18H3 F LEC01 20269",
                "Ethics of Artificial Intelligence",
            ),
            # No canvasbuddy header, but a colon mid-sentence: keep the whole thing.
            (
                "Building a Culture of Consent at the University of Toronto: Consent and More",
                "SVEP-BCC",
                "Building a Culture of Consent at the University of Toronto: Consent and More",
            ),
            ("Living in Residence", "UTSC-Living-in-Residence", "Living in Residence"),
            ("UTSC Management YES Challenge", None, "UTSC Management YES Challenge"),
        ],
    )
    def test_title(self, name: str, code: str | None, expected: str) -> None:
        assert course_title(name, code) == expected

    def test_empty_title_after_header_falls_back_to_the_raw_name(self) -> None:
        assert course_title("MGAB03H3 F LEC01 20269:", "MGAB03H3") == "MGAB03H3 F LEC01 20269:"
