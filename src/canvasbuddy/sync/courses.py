"""Term filtering and section matching.

Two problems live here, both discovered from live Canvas data rather than the docs.

**Term filtering.** ``enrollment_state=active`` returns far more than the current term:
finished courses from earlier terms whose enrolments are still open, plus permanent
non-term shells (orientation modules, residence, clubs). Only courses matching the
configured term are synced.

**Section matching.** Canvas supports section-specific due dates through assignment
overrides, but instructors frequently ignore that and create one assignment per
section instead -- "Group Project - L01" through "Group Project - L04", every one of
them visible to every student, with no overrides at all. Canvas exposes no field saying
which is yours, so the only available signal is the section suffix in the name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Section suffixes as instructors write them in assignment names: "- L04",
#: "(LEC 04)", "- Section 2", "- TUT0002".
_NAME_SUFFIX = re.compile(
    r"""[-–(\[\s]\s*
        (?P<kind>LEC|LECTURE|SEC|SECTION|TUT|TUTORIAL|PRA|PRACTICAL|L|S|T|P)
        \s*[-\s]?\s*
        (?P<number>\d{1,4})
        \s*[)\]]?\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: The same idea inside a Canvas section name: "MGAB03H3-F-LEC04-20269".
_SECTION_CODE = re.compile(
    r"(?P<kind>LEC|SEC|TUT|PRA|LECTURE|SECTION|TUTORIAL|PRACTICAL)\s*(?P<number>\d{1,4})",
    re.IGNORECASE,
)

#: Single letters are ambiguous on their own, so they are mapped explicitly.
_KIND_ALIASES = {
    "L": "LEC",
    "LEC": "LEC",
    "LECTURE": "LEC",
    "S": "LEC",
    "SEC": "LEC",
    "SECTION": "LEC",
    "T": "TUT",
    "TUT": "TUT",
    "TUTORIAL": "TUT",
    "P": "PRA",
    "PRA": "PRA",
    "PRACTICAL": "PRA",
}


@dataclass(frozen=True)
class SectionRef:
    """A section identity comparable across the two places it is written.

    ``L04`` in an assignment name and ``LEC04`` in a Canvas section name are the same
    section; normalising both to ``(LEC, 4)`` is what lets them be compared.
    """

    kind: str
    number: int

    def __str__(self) -> str:
        return f"{self.kind}{self.number:02d}"


def _normalize(kind: str, number: str) -> SectionRef | None:
    canonical = _KIND_ALIASES.get(kind.upper())
    if canonical is None:
        return None
    return SectionRef(canonical, int(number))


def parse_section_hint(assignment_name: str) -> SectionRef | None:
    """Extract the section a name is aimed at, if it names one.

    Returns None for the overwhelmingly common case of a name with no section suffix.
    """
    match = _NAME_SUFFIX.search(assignment_name.strip())
    if not match:
        return None
    return _normalize(match.group("kind"), match.group("number"))


def parse_section_name(section_name: str) -> SectionRef | None:
    """Extract the section identity from a Canvas section name."""
    match = _SECTION_CODE.search(section_name)
    if not match:
        return None
    return _normalize(match.group("kind"), match.group("number"))


def enrolled_section_refs(section_names: list[str] | None) -> set[SectionRef]:
    refs = set()
    for name in section_names or []:
        ref = parse_section_name(name)
        if ref is not None:
            refs.add(ref)
    return refs


def name_stem(assignment_name: str) -> str:
    """The assignment name with any section suffix stripped.

    Used to group the section variants of one logical assignment together.
    """
    return _NAME_SUFFIX.sub("", assignment_name.strip()).strip(" -–([").strip()


#: A course code with the trailing credit-weight and campus digits split off:
#: MGAB03H3 -> MGAB03 + H3, PHLB18H3 -> PHLB18 + H3. Canvas stores the long form,
#: but the short form is what anyone actually says out loud.
_COURSE_CODE = re.compile(r"^([A-Z]{2,6}\d{2,4})[A-Z]?\d?$")

#: The five-digit session code UofT appends to course names, e.g. 20269.
_SESSION_CODE = re.compile(r"\b\d{5}\b")


def short_course_code(course_code: str | None) -> str:
    """Trim a Canvas course code down to what a person would say.

    ``MGAB03H3 F LEC01 20269`` becomes ``MGAB03``. Anything that does not look like a
    UofT-style code is returned as-is rather than mangled, so this stays safe for other
    Canvas institutions.
    """
    if not course_code:
        return ""
    first = course_code.split()[0] if course_code.split() else course_code
    match = _COURSE_CODE.match(first)
    return match.group(1) if match else first


def course_title(name: str, course_code: str | None = None) -> str:
    """Recover the human-readable title from a Canvas course name.

    Canvas names arrive with the canvasbuddy's own header glued on the front:
    ``MGAB03H3 F LEC01 20269:Introductory Management Accounting``. Only the part after
    the colon is the title -- and only when the part *before* it is really a header,
    which is why this checks for a session code rather than blindly splitting. Titles
    legitimately contain colons ("Price Theory: A Mathematical Approach"), so the split
    is on the first one only.
    """
    head, separator, tail = name.partition(":")
    if not separator:
        return name.strip()
    looks_like_header = bool(_SESSION_CODE.search(head)) or (
        bool(course_code) and course_code.split()[0] in head
    )
    return tail.strip() if looks_like_header and tail.strip() else name.strip()


def is_tracked_term(term_name: str | None, configured_term: str) -> bool:
    """Whether a course belongs to the term being tracked.

    Canvas' "Default Term" is the catch-all for shells that are not real courses, so
    it never matches.
    """
    if not term_name:
        return False
    if term_name.strip().lower() == "default term":
        return False
    return term_name.strip().lower() == configured_term.strip().lower()
