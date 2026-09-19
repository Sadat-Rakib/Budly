"""Command-line surface.

``tick`` is the deployment entrypoint. Everything else exists so a human can inspect
and rehearse what ``tick`` will do before it does it unattended.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import UTC, datetime
from functools import wraps
from typing import Any

import typer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from canvasbuddy.canvas.client import CanvasClient, TokenRevokedError
from canvasbuddy.config import Settings, get_settings
from canvasbuddy.db import session_scope
from canvasbuddy.digest.builder import DigestContent, build_digest, render_digest
from canvasbuddy.models import Course, Digest
from canvasbuddy.sync.worker import sync_all

app = typer.Typer(
    no_args_is_help=True, add_completion=False, help="StudyBuddy — your term, in one place."
)
courses_app = typer.Typer(
    no_args_is_help=True, help="Inspect and override which courses are tracked."
)
app.add_typer(courses_app, name="courses")

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _force_utf8_output() -> None:
    """Print UTF-8 regardless of the console's code page.

    Windows terminals default to cp1252, which cannot encode the emoji the digest uses
    as section markers -- ``canvasbuddy digest --dry-run`` would die on a UnicodeEncodeError
    while the identical text sends to Telegram perfectly well. ``errors="replace"``
    keeps a console that genuinely cannot render a glyph from taking the command down
    with it.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover - redirected/closed stream
                pass


_force_utf8_output()


def _async_command(fn: Any) -> Any:
    """Let Typer commands be coroutines."""

    @wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# --------------------------------------------------------------------------- doctor


@app.command()
@_async_command
async def doctor() -> None:
    """Check every credential and connection before trusting the cron."""
    settings = get_settings()
    typer.echo(f"Canvas base URL:  {settings.canvas_base_url}")
    typer.echo(f"Tracking term:    {settings.canvas_term}")
    typer.echo(
        f"Timezone:         {settings.user_timezone}  (digest at {settings.digest_hour:02d}:00)"
    )
    typer.echo("")

    ok = True

    async with CanvasClient(settings) as client:
        try:
            me = await client.get_self()
            typer.secho(f"[ok]   Canvas token — {me.get('name')} (id {me.get('id')})", fg="green")
        except TokenRevokedError as exc:
            typer.secho(f"[FAIL] Canvas token — {exc}", fg="red")
            raise typer.Exit(1) from exc

        raw = await client.get_courses()
        matched = [
            c
            for c in raw
            if ((c.get("term") or {}).get("name") or "").strip().lower()
            == settings.canvas_term.strip().lower()
        ]
        typer.secho(
            f"[ok]   Courses — {len(matched)} in {settings.canvas_term}, {len(raw)} active overall",
            fg="green" if matched else "yellow",
        )
        for course in matched:
            code = (course.get("course_code") or "").split(" ")[0]
            typer.echo(f"         · {code:<10} {course.get('name', '')[:58]}")
        if not matched:
            ok = False
            typer.secho(
                f"       No course matched CANVAS_TERM={settings.canvas_term!r}. "
                "Terms Canvas reported: "
                + ", ".join(sorted({(c.get("term") or {}).get("name") or "?" for c in raw})),
                fg="yellow",
            )
        if client.rate_limit_remaining is not None:
            typer.echo(f"         quota remaining: {client.rate_limit_remaining:.0f}")

    try:
        async with session_scope() as session:
            count = len((await session.scalars(select(Course))).all())
        typer.secho(f"[ok]   Database — reachable, {count} courses stored", fg="green")
    except Exception as exc:  # noqa: BLE001 — doctor reports rather than raises
        ok = False
        typer.secho(f"[FAIL] Database — {type(exc).__name__}: {exc}", fg="red")

    if settings.telegram_configured:
        from canvasbuddy.channels.telegram import TelegramChannel

        try:
            TelegramChannel(settings)
            typer.secho("[ok]   Telegram — credentials present", fg="green")
        except Exception as exc:  # noqa: BLE001
            ok = False
            typer.secho(f"[FAIL] Telegram — {exc}", fg="red")
    else:
        typer.secho("[warn] Telegram — not configured; digests can only be printed", fg="yellow")

    if settings.openrouter_configured:
        from canvasbuddy.llm.openrouter import LLMError, ModelUnsupportedError, OpenRouterClient

        try:
            async with OpenRouterClient(settings) as llm:
                await llm.assert_supports_tools(settings.chat_model)
            typer.secho(f"[ok]   OpenRouter — {settings.chat_model} supports tools", fg="green")
        except ModelUnsupportedError as exc:
            ok = False
            typer.secho(f"[FAIL] OpenRouter — {exc}", fg="red")
        except LLMError as exc:
            ok = False
            typer.secho(f"[FAIL] OpenRouter — {exc}", fg="red")
    else:
        typer.secho("[warn] OpenRouter — not configured; chat disabled", fg="yellow")

    # --- StudyBuddy serverless checks ---
    try:
        for role, spec in (
            ("digest", settings.digest_slot),
            ("nudge", settings.nudge_slot),
            ("review", settings.review_slot),
            ("checkin", settings.checkin_slot),
        ):
            if spec.strip():
                from canvasbuddy.slots import parse_slot

                parse_slot(role, spec.strip())
        typer.secho("[ok]   Slots — digest/nudge/review/checkin parse", fg="green")
    except Exception as exc:  # noqa: BLE001
        ok = False
        typer.secho(f"[FAIL] Slots — {exc}", fg="red")

    if settings.slack_webhook_url:
        typer.secho("[ok]   Slack — webhook present (not tested; run setup to test)", fg="green")
    else:
        typer.secho("[warn] Slack — not configured; Telegram only", fg="yellow")

    if settings.webhook_secret and settings.cron_secret:
        typer.secho("[ok]   Webhook/cron secrets — present", fg="green")
    else:
        missing = [
            k
            for k, v in (
                ("WEBHOOK_SECRET", settings.webhook_secret),
                ("CRON_SECRET", settings.cron_secret),
            )
            if not v
        ]
        typer.secho(
            f"[warn] Secrets missing: {', '.join(missing)} — run `canvasbuddy setup`", fg="yellow"
        )

    raise typer.Exit(0 if ok else 1)


@app.command()
@_async_command
async def setup(
    check: bool = typer.Option(False, "--check", help="Re-validate existing .env, write nothing."),
) -> None:
    """Guided setup: prompts, validates, writes .env + vercel.env, migrates + syncs."""
    import os

    from canvasbuddy.setup import (
        build_env_text,
        check_canvas,
        check_database,
        check_slack_webhook,
        check_telegram_bot,
        detect_chat_id,
        generate_secret,
        validate_slot_input,
        validate_timezone,
        write_env_file,
    )

    if check:
        settings = get_settings()
        typer.echo(
            f"Canvas: {settings.canvas_base_url} "
            f"term={settings.canvas_term!r} tz={settings.user_timezone}"
        )
        typer.echo(
            f"Telegram configured: {settings.telegram_configured} "
            f"Slack: {settings.slack_configured}"
        )
        typer.echo(
            f"Slots: digest={settings.digest_slot!r} nudge={settings.nudge_slot!r} "
            f"review={settings.review_slot!r} checkin={settings.checkin_slot!r}"
        )
        missing = [
            k
            for k in ("CANVAS_TOKEN", "DATABASE_URL", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
            if not os.getenv(k) and not getattr(settings, k.lower(), None)
        ]
        if missing:
            typer.secho(f"Missing: {', '.join(missing)}", fg="yellow")
            raise typer.Exit(1)
        typer.secho("Setup check passed.", fg="green")
        return

    typer.echo(
        "StudyBuddy setup — ~30 min. Values are validated as you go. Secrets are never echoed back."
    )
    canvas_base = typer.prompt("Canvas base URL", default="https://canvas.ualberta.ca")
    canvas_term = typer.prompt("Canvas term (exact, e.g. Fall Term 2026)", default="Fall Term 2026")
    canvas_token = typer.prompt("Canvas access token", hide_input=True)
    try:
        info = await check_canvas(canvas_base, canvas_token)
        typer.secho(
            f"Canvas OK — {info['user'].get('name')} ({len(info['courses'])} courses)", fg="green"
        )
        terms = ", ".join(info["terms"][:8])
        typer.echo(f"Terms seen: {terms}")
        matched = [
            c
            for c in info["courses"]
            if ((c.get("term") or {}).get("name") or "").strip().lower()
            == canvas_term.strip().lower()
        ]
        typer.echo(f"{len(matched)} courses in {canvas_term!r}")
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"Canvas check failed: {exc}", fg="red")
        raise typer.Exit(1) from exc

    tg_token = typer.prompt("Telegram bot token (from @BotFather)", hide_input=True)
    try:
        me = await check_telegram_bot(tg_token)
        typer.secho(f"Bot OK — @{me.get('username')}", fg="green")
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"Telegram check failed: {exc}", fg="red")
        raise typer.Exit(1) from exc
    typer.echo("Message your bot once (tap Start), then press Enter...")
    typer.prompt("Ready", default="y")
    chat_id = await detect_chat_id(tg_token)
    if not chat_id:
        chat_id = typer.prompt("Could not auto-detect chat ID — paste it (from @userinfobot)")
    else:
        typer.secho(f"Detected chat ID ending …{chat_id[-4:]}", fg="green")

    slack_url = typer.prompt("Slack webhook URL (Enter to skip)", default="")
    if slack_url.strip():
        try:
            await check_slack_webhook(slack_url.strip())
            typer.secho("Slack OK — test message posted.", fg="green")
        except Exception as exc:  # noqa: BLE001
            typer.secho(f"Slack check failed: {exc}", fg="yellow")
            if not typer.confirm("Continue without Slack?", default=True):
                raise typer.Exit(1) from exc
            slack_url = ""

    db_url = typer.prompt("Supabase Session pooler DATABASE_URL (port 5432, *.pooler.supabase.com)")
    try:
        await check_database(db_url)
        typer.secho("Database OK.", fg="green")
    except Exception as exc:  # noqa: BLE001
        typer.secho(f"Database check failed: {exc}", fg="red")
        raise typer.Exit(1) from exc

    tz = typer.prompt("Timezone", default="America/Edmonton")
    while not validate_timezone(tz):
        typer.secho("Unknown timezone. Try America/Edmonton.", fg="yellow")
        tz = typer.prompt("Timezone", default="America/Edmonton")
    user_name = typer.prompt("Your first name (for messages)", default="Mir")
    digest_slot = typer.prompt("DIGEST_SLOT", default="daily@07:00")
    nudge_slot = typer.prompt("NUDGE_SLOT", default="daily@20:00")
    review_slot = typer.prompt("REVIEW_SLOT", default="sat@08:00")
    checkin_slot = typer.prompt("CHECKIN_SLOT", default="sat@15:00")
    try:
        digest_slot = validate_slot_input("digest", digest_slot)
        nudge_slot = validate_slot_input("nudge", nudge_slot)
        review_slot = validate_slot_input("review", review_slot)
        checkin_slot = validate_slot_input("checkin", checkin_slot)
    except ValueError as exc:
        typer.secho(f"Bad slot: {exc}", fg="red")
        raise typer.Exit(1) from exc

    webhook_secret = generate_secret()
    cron_secret = generate_secret()
    values = {
        "CANVAS_BASE_URL": canvas_base.strip(),
        "CANVAS_TOKEN": canvas_token.strip(),
        "CANVAS_TERM": canvas_term.strip(),
        "TELEGRAM_BOT_TOKEN": tg_token.strip(),
        "TELEGRAM_CHAT_ID": chat_id.strip(),
        "USER_TIMEZONE": tz.strip(),
        "USER_NAME": user_name.strip(),
        "DATABASE_URL": db_url.strip(),
        "DIGEST_SLOT": digest_slot,
        "NUDGE_SLOT": nudge_slot,
        "REVIEW_SLOT": review_slot,
        "CHECKIN_SLOT": checkin_slot,
        "NOTIFY_GRACE_MINUTES": "180",
        "REVIEW_REPLACES_DAILY": "true",
        "REVIEW_LOOKBACK_DAYS": "7",
        "REVIEW_LOOKAHEAD_DAYS": "7",
        "WEBHOOK_SECRET": webhook_secret,
        "CRON_SECRET": cron_secret,
        "SLACK_WEBHOOK_URL": slack_url.strip(),
    }
    write_env_file(".env", build_env_text(values))
    write_env_file("vercel.env", build_env_text(values))
    typer.secho("Wrote .env and vercel.env (0600 where supported).", fg="green")
    typer.echo("Next:")
    typer.echo("  uv run alembic upgrade head")
    typer.echo("  uv run canvasbuddy sync")
    typer.echo("  npm i -g vercel; vercel login; vercel link  # then import vercel.env")
    typer.echo("  vercel --prod")
    typer.echo(
        '  curl "https://api.telegram.org/bot<TOKEN>/setWebhook"'
        ' -d "url=https://<project>.vercel.app/api/telegram"'
        ' -d "secret_token=<WEBHOOK_SECRET>" -d "drop_pending_updates=true"'
    )
    typer.echo(
        "  cron-job.org: GET https://<project>.vercel.app/api/cron every 15 min,"
        " header Authorization: Bearer <CRON_SECRET>"
    )


# ----------------------------------------------------------------------------- sync


@app.command()
@_async_command
async def sync(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would change; write nothing."
    ),
) -> None:
    """Run one sync pass over Canvas."""
    settings = get_settings()
    async with CanvasClient(settings) as client, session_scope() as session:
        report = await sync_all(session, client, settings, dry_run=dry_run)

    typer.echo(report.summary())
    if report.events:
        typer.echo("")
        typer.echo("events:")
        for event in report.events[:40]:
            label = event.payload.get("name") or event.payload.get("title")
            typer.echo(f"  {event.type.value:<20} {label}")
        if len(report.events) > 40:
            typer.echo(f"  … and {len(report.events) - 40} more")
    if dry_run:
        typer.secho("\n(dry run — nothing was written)", fg="yellow")


# --------------------------------------------------------------------------- digest


async def _send_digest(
    session: AsyncSession,
    settings: Settings,
    content: DigestContent,
    body: str,
) -> bool:
    """Send and record. Returns False if today's digest already went out.

    The unique constraint on ``(local_date, channel, kind)`` is what makes this safe:
    two ticks racing cannot both send, because the second insert fails rather than the
    second send succeeding.
    """
    from canvasbuddy.channels.telegram import TelegramChannel

    channel = TelegramChannel(settings)
    row = Digest(
        local_date=content.local_date.date(),
        channel=channel.name,
        kind="morning",
        body_md=body,
        items={"announcement_ids": content.announcement_ids},
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return False

    await channel.send(body)
    return True


@app.command()
@_async_command
async def digest(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the digest instead of sending it."
    ),
    force: bool = typer.Option(
        False, "--force", help="Send even if today's digest already went out."
    ),
) -> None:
    """Build today's digest, and send it unless --dry-run."""
    settings = get_settings()
    async with session_scope() as session:
        content = await build_digest(session, settings)
        body = render_digest(content, settings)

        if dry_run:
            typer.echo(body)
            typer.secho("\n(dry run — nothing was sent)", fg="yellow")
            return

        if force:
            existing = await session.scalar(
                select(Digest).where(
                    Digest.local_date == content.local_date.date(), Digest.kind == "morning"
                )
            )
            if existing:
                await session.delete(existing)
                await session.flush()

        sent = await _send_digest(session, settings, content, body)

    if sent:
        typer.secho("Digest sent.", fg="green")
    else:
        typer.secho("Today's digest already went out; nothing sent.", fg="yellow")


# ----------------------------------------------------------------------------- tick


def should_send_digest(now_local: datetime, digest_hour: int, *, already_sent_today: bool) -> bool:
    """Whether this tick should send the morning digest.

    Computing the local hour here rather than encoding it in a crontab expression is
    what makes the schedule survive daylight saving: the cron fires on UTC, and the
    decision is made in the user's own timezone. It also makes a missed run self-heal,
    since the next tick after the hour still qualifies.
    """
    return not already_sent_today and now_local.hour >= digest_hour


@app.command()
@_async_command
async def tick() -> None:
    """Cron entrypoint: sync, then send the digest if it is due and unsent."""
    settings = get_settings()
    now_local = datetime.now(UTC).astimezone(settings.tz)

    try:
        async with CanvasClient(settings) as client, session_scope() as session:
            report = await sync_all(session, client, settings)
        typer.echo(report.summary())
    except TokenRevokedError as exc:
        # Silent failure is the worst outcome here: the token dies on a password change
        # and the digests simply stop, with nothing to notice.
        if settings.telegram_configured:
            from canvasbuddy.channels.telegram import TelegramChannel

            await TelegramChannel(settings).send_plain(
                f"⚠️ CanvasBuddy can't reach Canvas — the access token was rejected.\n\n{exc}"
            )
        raise typer.Exit(1) from exc

    async with session_scope() as session:
        already = await session.scalar(
            select(Digest).where(Digest.local_date == now_local.date(), Digest.kind == "morning")
        )
        if not should_send_digest(
            now_local, settings.digest_hour, already_sent_today=already is not None
        ):
            typer.echo(
                f"Digest not due yet (local time {now_local:%H:%M}, "
                f"scheduled {settings.digest_hour:02d}:00)."
                if already is None
                else "Today's digest already sent."
            )
            return

        content = await build_digest(session, settings)
        body = render_digest(content, settings)
        sent = await _send_digest(session, settings, content, body)

    typer.secho("Digest sent." if sent else "Digest already sent by a concurrent run.", fg="green")


@app.command("contacts")
@_async_command
async def contacts_cmd() -> None:
    """Harvest instructor and TA addresses out of the stored syllabus text."""
    from sqlalchemy import select

    from canvasbuddy.agent.contacts import harvest_contacts
    from canvasbuddy.models import Course

    async with session_scope() as session:
        courses = (
            await session.scalars(select(Course).where(Course.is_tracked, Course.is_active))
        ).all()
        found = []
        for course in courses:
            found.extend(await harvest_contacts(session, course))
        await session.flush()
        rows = [
            (c.email, c.role or "?", (c.source_quote or "")[:70], c.confirmed_by_user)
            for c in found
        ]

    if not rows:
        typer.echo("No new addresses found. Run `canvasbuddy extract` first if the syllabi")
        typer.echo("have not been read yet.")
        return
    for email, role, quote, confirmed in rows:
        mark = "confirmed" if confirmed else "UNCONFIRMED"
        typer.echo(f"  {email:<38} {role:<12} [{mark}]")
        typer.echo(f"      found in: {quote}")
    typer.secho(
        "\nCheck these before using them - an address from a document can be wrong,",
        fg="yellow",
    )
    typer.secho("and emailing the wrong professor is worse than having no address.", fg="yellow")


# ---------------------------------------------------------------------------- extract


@app.command()
@_async_command
async def extract(
    course: str = typer.Option(None, "--course", help="Limit to one course, e.g. MGAB03."),
    force: bool = typer.Option(False, "--force", help="Re-read documents even if unchanged."),
) -> None:
    """Pull syllabus documents from Canvas and extract the assessments in them."""
    from canvasbuddy.documents.pipeline import run_extraction
    from canvasbuddy.llm.openrouter import OpenRouterClient

    settings = get_settings()
    llm = OpenRouterClient(settings) if settings.openrouter_configured else None
    try:
        async with CanvasClient(settings) as client, session_scope() as session:
            report, results = await run_extraction(
                session, settings, client, llm, course_code=course, force=force
            )
    finally:
        if llm is not None:
            await llm.aclose()

    typer.echo(report.summary())
    typer.echo("")
    for result in results:
        typer.echo(result.summary())
    if not results:
        typer.secho("No assessments extracted.", fg="yellow")


# ------------------------------------------------------------------------------ serve


@app.command()
def serve() -> None:
    """Run the bot: Telegram polling plus the scheduled sync and digest.

    This is the always-on entrypoint. It replaces `tick` as the deployed command -- the
    same sync and digest run here, on the job queue, so there is no separate cron service.
    """
    from canvasbuddy.bot import run

    run()


# --------------------------------------------------------------------------- courses


@courses_app.command("list")
@_async_command
async def courses_list() -> None:
    """Show every stored course, how it will be labelled, and whether it is tracked."""
    async with session_scope() as session:
        rows = (await session.scalars(select(Course).order_by(Course.code))).all()
    if not rows:
        typer.echo("No courses stored yet. Run `canvasbuddy sync` first.")
        return
    for course in rows:
        mark = "✓" if course.is_tracked else " "
        star = "*" if course.nickname else " "
        code = course.short_code or course.code
        name = course.nickname or course.title or course.name
        typer.echo(f" [{mark}] {code:<10}{star} {(course.term_name or '—'):<14} {name[:48]}")
    typer.echo('\n  * = your own name. Set one with: canvasbuddy courses name <CODE> "<name>"')


@courses_app.command("name")
@_async_command
async def courses_name(
    code: str,
    name: str = typer.Argument(..., help='e.g. "Managerial Accounting". Pass "" to clear.'),
) -> None:
    """Give a course your own name, used everywhere in the digest.

    Survives every sync: the parsed title is recomputed each pass, but a name you set
    here is never overwritten.
    """
    async with session_scope() as session:
        course = await session.scalar(
            select(Course).where((Course.short_code == code) | (Course.code == code))
        )
        if course is None:
            typer.secho(f"No course matching {code!r}. Try `canvasbuddy courses list`.", fg="red")
            raise typer.Exit(1)
        course.nickname = name or None
        label = course.label
    typer.secho(label, fg="green")


async def _set_tracked(code: str, tracked: bool) -> None:
    async with session_scope() as session:
        course = await session.scalar(
            select(Course).where((Course.short_code == code) | (Course.code == code))
        )
        if course is None:
            typer.secho(f"No course with code {code!r}. Try `canvasbuddy courses list`.", fg="red")
            raise typer.Exit(1)
        course.is_tracked = tracked
    typer.secho(f"{code} is now {'tracked' if tracked else 'untracked'}.", fg="green")


@courses_app.command("track")
@_async_command
async def courses_track(code: str) -> None:
    """Track a course the term filter excluded."""
    await _set_tracked(code, True)


@courses_app.command("untrack")
@_async_command
async def courses_untrack(code: str) -> None:
    """Stop tracking a course."""
    await _set_tracked(code, False)


if __name__ == "__main__":
    app()
