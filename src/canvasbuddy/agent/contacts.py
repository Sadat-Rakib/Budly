"""Instructor and TA addresses, and drafting mail to them.

Two halves, deliberately separated:

* **Finding addresses.** Syllabus text contains instructor and TA emails. Extraction can
  be wrong, and emailing the wrong professor is worse than not having the address, so an
  address is stored unconfirmed and shown with the line it came from before it is used.
* **Drafting.** The bot writes the mail. It does not send it. Sending on someone's behalf
  to a third party is not something to automate, and the practical route -- opening the
  draft in your own mail client -- also keeps you as the sender, which is what a
  professor's spam filter wants to see.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from canvasbuddy.config import Settings
from canvasbuddy.llm.openrouter import OpenRouterClient
from canvasbuddy.models import Contact, Course, File

log = logging.getLogger(__name__)

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")

#: Addresses that are never a person to contact about coursework.
_IGNORE = re.compile(
    r"(no-?reply|do-?not-?reply|support@|help@|info@|@wiley|@pearson|@mcgraw|@example)",
    re.IGNORECASE,
)

_ROLE_HINTS = (
    ("instructor", ("instructor", "professor", "lecturer", "course director")),
    ("ta", ("teaching assistant", "ta:", "t.a.", "tutorial leader")),
    ("coordinator", ("coordinator", "administrator", "admin")),
)


def _role_near(text: str, position: int) -> str | None:
    """Guess a role from the words immediately around an address."""
    window = text[max(0, position - 220) : position + 80].lower()
    for role, hints in _ROLE_HINTS:
        if any(h in window for h in hints):
            return role
    return None


def _line_around(text: str, position: int) -> str:
    start = text.rfind("\n", 0, position) + 1
    end = text.find("\n", position)
    line = text[start : end if end != -1 else position + 160]
    return " ".join(line.split())[:300]


async def harvest_contacts(session: AsyncSession, course: Course) -> list[Contact]:
    """Pull candidate addresses out of a course's stored syllabus text.

    Everything found is unconfirmed. The line it came from is kept so the decision to
    trust it can be made on evidence rather than on faith.
    """
    rows = (
        await session.scalars(
            select(File).where(File.course_id == course.id, File.extracted_text.is_not(None))
        )
    ).all()

    known = {
        c.email.lower()
        for c in (
            await session.scalars(select(Contact).where(Contact.course_id == course.id))
        ).all()
    }

    created: list[Contact] = []
    for row in rows:
        text = row.extracted_text or ""
        for match in _EMAIL.finditer(text):
            email = match.group(0)
            if _IGNORE.search(email) or email.lower() in known:
                continue
            contact = Contact(
                course_id=course.id,
                email=email,
                role=_role_near(text, match.start()),
                source="syllabus",
                source_quote=_line_around(text, match.start()),
                confirmed_by_user=False,
            )
            session.add(contact)
            created.append(contact)
            known.add(email.lower())
    return created


_DRAFT_SYSTEM = """\
You draft short emails from a university student to course staff.

Rules:
- Three or four sentences. Nobody wants to read a long email from a student.
- Polite, plain, direct. No "I hope this email finds you well". No flattery.
- Say who is writing and which course in the first sentence.
- Ask one clear thing. If a deadline or assignment is involved, name it exactly.
- Sign off with the student's name only.

Return ONLY JSON: {"subject": "...", "body": "..."}
"""


async def draft_email(
    settings: Settings,
    llm: OpenRouterClient,
    *,
    student_name: str,
    course_label: str,
    recipient: str,
    request: str,
    context: str | None = None,
) -> dict:
    """Write a subject and body. Returns {"subject", "body"}."""
    import json

    detail = f"\n\nRelevant course information:\n{context}" if context else ""
    choice = await llm.chat(
        [
            {"role": "system", "content": _DRAFT_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"Student: {student_name}\n"
                    f"Course: {course_label}\n"
                    f"Writing to: {recipient}\n"
                    f"What they want to say: {request}{detail}"
                ),
            },
        ],
        tools=None,
        max_tokens=800,
    )

    raw = (choice.get("message", {}).get("content") or "").strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        payload = json.loads(match.group(0)) if match else {"subject": "", "body": raw}
    return {
        "subject": (payload.get("subject") or "").strip(),
        "body": (payload.get("body") or "").strip(),
    }


def mailto_link(to: str, subject: str, body: str) -> str:
    """A mailto: URL that opens the draft in the user's own mail client.

    This is the send path. It keeps the message coming from the user's real address --
    which is what a university spam filter expects, and what makes replies land in their
    own inbox -- and it keeps a human between the model and a professor.
    """
    return f"mailto:{quote(to)}?subject={quote(subject)}&body={quote(body)}"
