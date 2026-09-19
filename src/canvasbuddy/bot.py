"""The always-on process: Telegram polling plus the scheduled sync and digest.

One process rather than two. ``python-telegram-bot[job-queue]`` runs APScheduler on the
same event loop that serves polling, started and stopped by ``run_polling()``, so the
15-minute sync and the 07:00 digest need no separate service and no separate scheduler.

Updates are processed sequentially, which is PTB's default and the right choice for a
single-user bot: it serialises conversation state without any locking. The cost is that a
slow tool loop looks like a hang, which is what the typing indicator is for.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from datetime import UTC, datetime, time, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)

from canvasbuddy.agent.loop import run_agent
from canvasbuddy.agent.memory import load_history, maybe_summarize, save_turn
from canvasbuddy.agent.prompt import build_system_prompt
from canvasbuddy.canvas.client import CanvasClient, TokenRevokedError
from canvasbuddy.config import Settings, get_settings
from canvasbuddy.db import get_engine, session_scope
from canvasbuddy.digest.builder import build_digest, render_digest
from canvasbuddy.digest.render import escape_md2, strip_markdown
from canvasbuddy.llm.openrouter import LLMError, OpenRouterClient
from canvasbuddy.settings_store import is_muted, mark_sent_today, mute_until, unmute
from canvasbuddy.sync.worker import sync_all

log = logging.getLogger(__name__)

CHANNEL = "telegram"
_SYNC_INTERVAL_SECONDS = 15 * 60
_TYPING_REFRESH_SECONDS = 4.0


# ------------------------------------------------------------------ authorisation


async def _reject_strangers(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop every update that is not from the authorised chat.

    Registered in ``group=-1`` so it runs before anything else. A per-handler
    ``filters.Chat(...)`` is the idiomatic mechanism but only covers message handlers --
    it would silently miss a ``CallbackQueryHandler`` added later for P2's exam
    confirmation buttons. A single choke point fails closed as handlers accumulate.

    A bot token that leaks will be probed, so rejections are logged.
    """
    settings: Settings = context.application.bot_data["settings"]
    chat = update.effective_chat
    if chat is None or str(chat.id) != str(settings.telegram_chat_id):
        log.warning("Ignoring update from unauthorised chat %s", chat.id if chat else "unknown")
        raise ApplicationHandlerStop


# ----------------------------------------------------------------------- helpers


@contextlib.asynccontextmanager
async def _typing(update: Update):
    """Keep the 'typing…' indicator alive for the duration of a slow reply.

    Telegram clears it after about five seconds, so it has to be refreshed or the bot
    looks dead in the middle of a tool loop.
    """
    chat = update.effective_chat

    async def loop() -> None:
        while True:
            with contextlib.suppress(Exception):
                await chat.send_action(ChatAction.TYPING)
            await asyncio.sleep(_TYPING_REFRESH_SECONDS)

    task = asyncio.create_task(loop()) if chat else None
    try:
        yield
    finally:
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


async def _reply(update: Update, text: str, *, markdown: bool = False) -> None:
    if markdown:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)
        return
    # Sent without a parse mode, so any Markdown the model produced would arrive as
    # literal asterisks. The system prompt asks it not to; this is the safety net for
    # when it does anyway.
    await update.effective_message.reply_text(strip_markdown(text))


# ---------------------------------------------------------------------- commands


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(
        update,
        "StudyBuddy — your Canvas courses in Telegram and Slack.\n"
        "Morning digest, evening nudge (only when due), Saturday review + check-in.\n\n"
        "Ask me anything about your courses — what's due, what a prof posted, how loaded "
        "next week looks.\n\n"
        "Shortcuts that skip the AI (faster, free):\n"
        "/today — due today\n"
        "/week — the next 7 days\n"
        "/grades — what's been marked\n"
        "/sync — pull from Canvas now\n"
        "/digest — today's digest again\n"
        "/exams — exam countdown\n"
        "/extract — re-read the syllabi\n"
        "/ics — calendar file\n"
        "/add <thing> — track something Canvas has no idea about\n"
        "/testnotify [digest|nudge|review|checkin] — send that briefing now to all channels\n"
        "/mute 3d — stop unprompted messages\n\n"
        "Voice notes work too.",
    )


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _upcoming(update, context, days=1, heading="Due today")


async def cmd_week(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _upcoming(update, context, days=7, heading="Next 7 days")


async def _upcoming(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, days: int, heading: str
) -> None:
    from canvasbuddy.agent.tools import list_upcoming

    settings: Settings = context.application.bot_data["settings"]
    async with session_scope() as session:
        data = await list_upcoming(session, settings, days=days)

    rows = data.get("due", [])
    if not rows:
        await _reply(update, f"{heading}: nothing.")
        return

    lines = [f"{heading}:"]
    for row in rows:
        when = (row.get("due_at") or "").replace("T", " ")
        mark = "✓" if row.get("submitted") else "·"
        lines.append(f"{mark} {row['course']} — {row['title']} ({when})")
    await _reply(update, "\n".join(lines))


async def cmd_grades(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from canvasbuddy.agent.tools import get_grades

    settings: Settings = context.application.bot_data["settings"]
    async with session_scope() as session:
        data = await get_grades(session, settings)

    if not data.get("graded"):
        await _reply(update, "Nothing has been graded yet.")
        return

    lines = [
        f"{r['course']} — {r['title']}: {r['score']}/{r['points_possible']}" for r in data["graded"]
    ]
    if data.get("percent") is not None:
        lines.append(
            f"\nOverall: {data['points_earned']}/{data['points_out_of']} ({data['percent']}%)"
        )
    await _reply(update, "\n".join(lines))


async def cmd_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    async with _typing(update):
        async with CanvasClient(settings) as client, session_scope() as session:
            report = await sync_all(session, client, settings)
    await _reply(update, report.summary())


async def cmd_digest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    async with session_scope() as session:
        content = await build_digest(session, settings)
        body = render_digest(content, settings)
    await _reply(update, body, markdown=True)


async def cmd_exams(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from canvasbuddy.agent.tools import get_exams

    settings: Settings = context.application.bot_data["settings"]
    async with session_scope() as session:
        data = await get_exams(session, settings)

    rows = data.get("exams") or []
    if not rows:
        await _reply(update, "No exams on record yet. Try /extract to read the syllabi.")
        return

    lines = []
    for row in rows:
        when = f"{row['days_away']} days" if row["days_away"] is not None else "date TBD"
        mark = "" if row["confirmed"] else "  (unconfirmed)"
        weight = f" · {row['weight_pct']:g}%" if row.get("weight_pct") else ""
        lines.append(f"{row['course']} — {row['title']}: {when}{weight}{mark}")
    await _reply(update, "\n".join(lines))


async def cmd_extract(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-read the syllabi and extract assessments, then ask about the doubtful ones."""
    settings: Settings = context.application.bot_data["settings"]
    llm: OpenRouterClient | None = context.application.bot_data.get("llm")
    if llm is None:
        await _reply(update, "Can't do that without OPENROUTER_API_KEY set.")
        return

    from canvasbuddy.documents.pipeline import run_extraction

    async with _typing(update):
        async with CanvasClient(settings) as client, session_scope() as session:
            report, results = await run_extraction(session, settings, client, llm)
            await session.flush()
            pending = await _pending_exams(session)

    total = sum(len(r.created) for r in results)
    await _reply(update, f"Read the syllabi. Found {total} assessment(s).\n\n{report.summary()}")
    for _exam_id, text, buttons in pending:
        await update.effective_message.reply_text(text, reply_markup=buttons)


async def _pending_exams(session) -> list[tuple[int, str, InlineKeyboardMarkup]]:
    """Anything the model was not confident about, packaged for confirmation.

    The verbatim source quote goes in the message: the decision is only meaningful if you
    can see the sentence the date was taken from.
    """
    from sqlalchemy import select

    from canvasbuddy.models import Course, Exam

    rows = (
        await session.scalars(select(Exam).where(~Exam.confirmed_by_user).order_by(Exam.date))
    ).all()
    out = []
    for row in rows:
        course = await session.get(Course, row.course_id)
        code = (course.short_code or course.code) if course else "?"
        facts = [str(row.date) if row.date else "no date"]
        if row.weight_pct is not None:
            facts.append(f"{float(row.weight_pct):g}%")
        if row.location:
            facts.append(row.location)

        lines = [f"{code} — {row.title}", " · ".join(facts)]
        if row.source_quote:
            lines += ["", "From the syllabus:", f'"{row.source_quote[:400]}"']
        if row.confidence is not None:
            lines += ["", f"Confidence {float(row.confidence):.0%}"]
        text = "\n".join(lines)
        buttons = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Confirm", callback_data=f"exam:ok:{row.id}"),
                    InlineKeyboardButton("Discard", callback_data=f"exam:no:{row.id}"),
                ]
            ]
        )
        out.append((row.id, text, buttons))
    return out


async def on_exam_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Confirm or discard an extracted assessment.

    Reached only for the authorised chat: the group=-1 TypeHandler covers callback
    queries, which a per-handler chat filter would not have.
    """
    from canvasbuddy.models import Exam

    query = update.callback_query
    await query.answer()
    try:
        _, action, raw_id = (query.data or "").split(":")
        exam_id = int(raw_id)
    except (ValueError, AttributeError):
        return

    async with session_scope() as session:
        exam = await session.get(Exam, exam_id)
        if exam is None:
            await query.edit_message_text("That one's already gone.")
            return
        title = exam.title
        if action == "ok":
            exam.confirmed_by_user = True
            verdict = f"✓ {title} — confirmed, it'll show in your digest."
        else:
            await session.delete(exam)
            verdict = f"✗ {title} — discarded."

    await query.edit_message_text(verdict)


# ------------------------------------------------------------------ conversation


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.effective_message.text or "").strip()
    if text:
        await _answer(update, context, text)


async def _answer(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    """Run one conversational turn.

    Shared by typed messages and transcribed voice notes: once a voice note is text,
    there is nothing about it that should behave differently.
    """
    settings: Settings = context.application.bot_data["settings"]
    llm: OpenRouterClient | None = context.application.bot_data.get("llm")

    if llm is None:
        await _reply(
            update,
            "I can't answer questions yet — OPENROUTER_API_KEY isn't set. "
            "The /today, /week and /grades shortcuts still work.",
        )
        return

    async with _typing(update):
        try:
            async with session_scope() as session:
                system = await build_system_prompt(session, settings)
                history = await load_history(session, settings, CHANNEL)
                messages = [
                    {"role": "system", "content": system},
                    *history,
                    {"role": "user", "content": text},
                ]
                reply = await run_agent(session, settings, llm, messages)

                await save_turn(session, CHANNEL, "user", text)
                await save_turn(session, CHANNEL, "assistant", reply.text)
        except LLMError as exc:
            log.exception("Agent turn failed")
            await _reply(update, f"That didn't work: {exc}")
            return

    log.info("Answered in %d round(s), tools: %s", reply.iterations, reply.tools_used or "none")
    await _reply(update, reply.text or "I don't have an answer for that.")

    # Summarising is a second model call, so it happens after the user has their reply.
    with contextlib.suppress(Exception):
        async with session_scope() as session:
            await maybe_summarize(session, settings, llm, CHANNEL)


async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Transcribe a voice note and answer it like any other message.

    Telegram delivers OGG/Opus, which OpenRouter accepts as an `input_audio` part
    directly, so the bytes go from Telegram to the model untouched.
    """
    from canvasbuddy.agent.transcribe import TranscriptionError, transcribe

    settings: Settings = context.application.bot_data["settings"]
    llm: OpenRouterClient | None = context.application.bot_data.get("llm")
    if llm is None:
        await _reply(update, "I can't do voice without OPENROUTER_API_KEY set.")
        return

    message = update.effective_message
    voice = message.voice or message.audio
    if voice is None:
        return

    async with _typing(update):
        try:
            handle = await context.bot.get_file(voice.file_id)
            audio = bytes(await handle.download_as_bytearray())
            text = await transcribe(settings, llm, audio, audio_format="ogg")
        except TranscriptionError as exc:
            await _reply(update, str(exc))
            return
        except Exception:
            log.exception("Voice transcription failed")
            await _reply(update, "Something went wrong transcribing that.")
            return

    # Echo the transcript so a misheard word is obvious rather than mysterious.
    await _reply(update, f"🎤 {text}")
    await _answer(update, context, text)


async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/mute 3d — stop unprompted messages for a while. Replies still work."""
    raw = (context.args[0] if context.args else "1d").strip().lower()
    match = re.fullmatch(r"(\d+)\s*([dhm])", raw)
    if not match:
        await _reply(update, "Try /mute 3d, /mute 12h or /mute off.")
        return
    amount, unit = int(match.group(1)), match.group(2)
    delta = {
        "d": timedelta(days=amount),
        "h": timedelta(hours=amount),
        "m": timedelta(minutes=amount),
    }[unit]

    settings: Settings = context.application.bot_data["settings"]
    until = datetime.now(UTC) + delta
    async with session_scope() as session:
        await mute_until(session, until)
    local = until.astimezone(settings.tz)
    await _reply(
        update,
        f"Muted until {local:%a %d %b, %H:%M}. Ask me things any time — muting only "
        "stops me starting conversations.",
    )


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    async with session_scope() as session:
        await unmute(session)
    await _reply(update, "Unmuted.")


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/add <text> — record something Canvas doesn't know about."""
    from canvasbuddy.agent.tools import add_manual_item

    settings: Settings = context.application.bot_data["settings"]
    text = " ".join(context.args or []).strip()
    if not text:
        await _reply(update, "Give me something to add, e.g. /add MGAB03 tutorial prep friday 5pm")
        return
    async with session_scope() as session:
        result = await add_manual_item(session, settings, title=text)
    await _reply(update, result.get("added") or "Added.")


async def cmd_ics(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/ics — every deadline and exam as an importable calendar file."""
    from io import BytesIO

    from canvasbuddy.digest.calendar import build_ics

    settings: Settings = context.application.bot_data["settings"]
    async with _typing(update), session_scope() as session:
        body = await build_ics(session, settings)

    buffer = BytesIO(body.encode())
    buffer.name = "studybuddy.ics"
    await update.effective_message.reply_document(
        document=buffer,
        filename="studybuddy.ics",
        caption="Import this into your calendar. Re-run /ics after things change.",
    )


async def cmd_testnotify(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/testnotify [digest|nudge|review|checkin] — render and send now, bypass schedule."""
    from datetime import UTC, datetime

    from canvasbuddy.digest.builder import build_digest, render_digest
    from canvasbuddy.notify.builders import (
        build_checkin_content,
        build_nudge_content,
        build_review_content,
        render_digest_slack,
    )
    from canvasbuddy.notify.notifiers import (
        SlackNotifier,
        render_slack_chunks,
        render_telegram_chunks,
    )

    settings: Settings = context.application.bot_data["settings"]
    arg = (context.args[0] if context.args else "").strip().lower()
    if arg not in ("digest", "nudge", "review", "checkin"):
        # Default: the role that would be due next (simple heuristic by weekday/time).
        now_local = datetime.now(UTC).astimezone(settings.tz)
        if now_local.weekday() == 5 and now_local.hour < 12:
            arg = "review"
        elif now_local.weekday() == 5 and now_local.hour >= 12:
            arg = "checkin"
        elif now_local.hour < 12:
            arg = "digest"
        else:
            arg = "nudge"

    async with _typing(update), session_scope() as session:
        now = datetime.now(UTC)
        tg_chunks: list[str] = []
        sl_chunks: list[str] = []
        if arg == "digest":
            content = await build_digest(session, settings, now=now)
            tg_chunks = [render_digest(content, settings)]
            sl_chunks = [render_digest_slack(content, settings)]
        elif arg == "nudge":
            nc = await build_nudge_content(session, settings, now=now)
            if nc is None:
                await _reply(update, "Nudge: nothing due in the next 24h — nothing sent.")
                return
            tg_chunks = render_telegram_chunks(nc)
            sl_chunks = render_slack_chunks(nc)
        elif arg == "review":
            nc = await build_review_content(session, settings, now=now)
            tg_chunks = render_telegram_chunks(nc)
            sl_chunks = render_slack_chunks(nc)
        else:
            nc = await build_checkin_content(session, settings, now=now)
            tg_chunks = render_telegram_chunks(nc)
            sl_chunks = render_slack_chunks(nc)

    # Send to Telegram via this chat (bypass idempotency, no digests row).
    for chunk in tg_chunks:
        try:
            await update.effective_message.reply_text(chunk, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            from canvasbuddy.digest.render import strip_markdown

            await update.effective_message.reply_text(strip_markdown(chunk))
    # Fan-out to Slack if configured (best-effort, report result).
    slack_result = "skipped (no SLACK_WEBHOOK_URL)"
    if settings.slack_webhook_url:
        try:
            notifier = SlackNotifier(settings)
            for chunk in sl_chunks:
                await notifier.send(chunk)
            slack_result = "sent"
        except Exception as exc:  # noqa: BLE001
            slack_result = f"failed: {exc}"
    await _reply(update, f"/testnotify {arg}: Telegram sent, Slack {slack_result}.")


# ---------------------------------------------------------------- scheduled jobs


async def job_sync(context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    try:
        async with CanvasClient(settings) as client, session_scope() as session:
            report = await sync_all(session, client, settings)
        log.info("Scheduled sync: %s", report.summary().replace("\n", " | "))
    except TokenRevokedError as exc:
        # Silent failure is the worst outcome: the token dies on a password change and
        # the digests simply stop, with nothing to notice.
        await context.bot.send_message(
            chat_id=settings.telegram_chat_id,
            text=f"⚠️ Can't reach Canvas — the access token was rejected.\n\n{exc}",
        )
    except Exception:
        log.exception("Scheduled sync failed")


async def _should_send_now(session, settings: Settings) -> tuple[bool, str]:
    """Whether the morning digest is due, and why."""
    local_now = datetime.now(UTC).astimezone(settings.tz)
    if local_now.hour >= settings.digest_hour:
        return True, f"{settings.digest_hour}:00"
    return False, "not due yet"


async def job_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the morning digest, at most once a day.

    The unique constraint on ``digests`` is what makes this safe to run repeatedly; the
    checks here only avoid the wasted work.
    """
    from sqlalchemy import select

    from canvasbuddy.models import Digest

    settings: Settings = context.application.bot_data["settings"]
    local_date = datetime.now(UTC).astimezone(settings.tz).date()

    async with session_scope() as session:
        if await is_muted(session):
            return
        already = await session.scalar(
            select(Digest).where(Digest.local_date == local_date, Digest.kind == "morning")
        )
        if already is not None:
            return

        due, why = await _should_send_now(session, settings)
        if not due:
            return

        content = await build_digest(session, settings)
        body = render_digest(content, settings)
        session.add(
            Digest(
                local_date=local_date,
                channel=CHANNEL,
                kind="morning",
                body_md=body,
                items={"announcement_ids": content.announcement_ids, "trigger": why},
            )
        )
        await session.flush()
        await context.bot.send_message(
            chat_id=settings.telegram_chat_id, text=body, parse_mode=ParseMode.MARKDOWN_V2
        )
    log.info("Digest sent for %s (%s)", local_date, why)


async def job_nudge(context: ContextTypes.DEFAULT_TYPE) -> None:
    """The evening nudge: one line, only if something is due within 24 hours.

    The PRD is emphatic that this is the *only* other unsolicited message. More than
    that and the bot gets muted inside a week, which costs the digest too.
    """
    from canvasbuddy.agent.tools import list_upcoming
    from canvasbuddy.settings_store import already_sent_today

    settings: Settings = context.application.bot_data["settings"]
    local_date = datetime.now(UTC).astimezone(settings.tz).date().isoformat()

    async with session_scope() as session:
        if await is_muted(session):
            return
        if await already_sent_today(session, "nudge_sent_on", local_date):
            return
        data = await list_upcoming(session, settings, days=1)
        pending = [row for row in data.get("due", []) if not row.get("submitted")]
        if not pending:
            return
        await mark_sent_today(session, "nudge_sent_on", local_date)

    if len(pending) == 1:
        row = pending[0]
        text = f"Due tomorrow: {row['course']} - {row['title']}"
    else:
        lines = "\n".join(f"- {r['course']} - {r['title']}" for r in pending)
        text = "Due in the next 24h:\n" + lines
    await context.bot.send_message(chat_id=settings.telegram_chat_id, text=text)


async def job_week_ahead(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sunday evening look-ahead at the coming week."""
    from canvasbuddy.agent.tools import workload_forecast
    from canvasbuddy.settings_store import already_sent_today

    settings: Settings = context.application.bot_data["settings"]
    local_now = datetime.now(UTC).astimezone(settings.tz)
    if local_now.weekday() != 6:  # Sunday
        return
    local_date = local_now.date().isoformat()

    async with session_scope() as session:
        if await is_muted(session):
            return
        if await already_sent_today(session, "week_ahead_sent_on", local_date):
            return
        data = await workload_forecast(session, settings, week_offset=1, weeks=1)
        await mark_sent_today(session, "week_ahead_sent_on", local_date)

    week = (data.get("weeks") or [{}])[0]
    items = week.get("items") or []
    if not items:
        text = "Nothing due next week. Genuinely nothing."
    else:
        lines = "\n".join(f"- {i['course']} - {i['title']}" for i in items[:10])
        text = f"Next week: {week['item_count']} things, {week['total_points']:g} points.\n" + lines
    await context.bot.send_message(chat_id=settings.telegram_chat_id, text=text)


# ------------------------------------------------------------------- application


async def _post_init(app: Application) -> None:
    settings: Settings = app.bot_data["settings"]
    if settings.openrouter_configured:
        llm = OpenRouterClient(settings)
        # Fail loudly at boot rather than with a bare 404 the first time you ask a
        # question. OpenRouter's catalogue moves.
        await llm.assert_supports_tools(settings.chat_model)
        app.bot_data["llm"] = llm
        log.info("Chat agent ready on %s", settings.chat_model)
    else:
        log.warning("OPENROUTER_API_KEY not set — slash commands only, no chat.")


async def _post_shutdown(app: Application) -> None:
    llm: OpenRouterClient | None = app.bot_data.get("llm")
    if llm is not None:
        await llm.aclose()
    await get_engine().dispose()
    log.info("Shut down cleanly.")


def build_application(settings: Settings | None = None) -> Application:
    settings = settings or get_settings()
    if not settings.telegram_configured:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must both be set.")
    assert settings.telegram_bot_token is not None

    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token.get_secret_value())
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )
    app.bot_data["settings"] = settings

    app.add_handler(TypeHandler(Update, _reject_strangers), group=-1)

    app.add_handler(CommandHandler(["start", "help"], cmd_help))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("week", cmd_week))
    app.add_handler(CommandHandler("grades", cmd_grades))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("exams", cmd_exams))
    app.add_handler(CommandHandler("extract", cmd_extract))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("ics", cmd_ics))
    app.add_handler(CommandHandler("testnotify", cmd_testnotify))
    app.add_handler(CallbackQueryHandler(on_exam_button, pattern=r"^exam:"))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    queue = app.job_queue
    if queue is not None:
        queue.run_repeating(job_sync, interval=_SYNC_INTERVAL_SECONDS, first=10)
        # run_daily takes a timezone-aware time, so the 07:00 contract survives DST
        # without any of the arithmetic a UTC cron would have needed.
        queue.run_daily(job_digest, time=time(hour=settings.digest_hour, tzinfo=settings.tz))
        # A safety net: if the daily job is missed (a redeploy at 06:59, say), the next
        # hourly pass still sends it. The idempotency check makes this free.
        queue.run_repeating(job_digest, interval=3600, first=120)
        queue.run_daily(job_nudge, time=time(hour=settings.nudge_hour, tzinfo=settings.tz))
        queue.run_daily(job_week_ahead, time=time(hour=19, tzinfo=settings.tz))

    return app


def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app = build_application()
    # drop_pending_updates: on a redeploy the bot must not replay a backlog of questions
    # against data that has since moved on.
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


def escape(text: str) -> str:
    """Re-exported so handlers can escape ad-hoc MarkdownV2 without a second import."""
    return escape_md2(text)
