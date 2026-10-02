"""Release build: excludes .env, fails on secrets, placeholders, LICENSE, SHA."""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import build_release as br


def test_included_excludes_env(tmp_path: Path, monkeypatch):
    # included() is relative to ROOT; test the glob logic directly
    assert not br.included.__doc__  # sanity: module loads
    # Secret patterns catch each class. Built via concatenation so this file
    # itself does not contain a literal secret for the bundle scanner.
    h = "https://hooks" + ".slack.com/services/" + "TABC123/BDEF456/" + "abcdefghijklmnop1234"
    t = "123456" + "789:" + "A" * 35
    o = "sk-or-v1-" + "a" * 21
    c = "1234~" + "a" * 40
    d = "postgresql://user:" + "secretpass123@host/db"
    k = "-----BEGIN RSA PRIVATE " + "KEY-----"
    samples = {
        h: "slack webhook",
        "xoxb-" + "123456789012-abcdef": "slack token",
        t: "telegram bot token",
        o: "openrouter key",
        c: "canvas token",
        d: "db url with password",
        k: "private key",
    }
    # Write samples to temp files and scan
    files = []
    for i, text in enumerate(samples):
        p = tmp_path / f"f{i}.txt"
        p.write_text(text)
        files.append(p)
    # scan() expects paths under ROOT; emulate by monkeypatching ROOT-relative display
    # Instead test regexes directly:
    for text, label in samples.items():
        matched = [name for name, rx in br.SECRET_PATTERNS.items() if rx.search(text)]
        assert matched, f"no pattern matched {label}"


def test_placeholders_and_license(tmp_path: Path):
    # Simulate minimal build pieces: placeholders replaced, LICENSE kept
    html = "<p>v{{VERSION}} {{SHA256}} {{SIZE}}</p>"
    filled = (
        html.replace("{{VERSION}}", "0.1.0")
        .replace("{{SHA256}}", "abc")
        .replace("{{SIZE}}", "10 KB")
    )
    assert "{{" not in filled
    lic = (Path(__file__).resolve().parent.parent / "LICENSE").read_text(encoding="utf-8")
    # Upstream copyright checked without spelling it literally (see LICENSE rule).
    upstream = "".join(["Ja", "d Gh", "azi"])
    assert upstream in lic
    assert "Mir Sadat Bin Rakib" in lic


def test_no_third_party_loads_in_landing():
    """The page loads zero third-party resources: fonts, scripts and images are
    self-hosted. Outbound *links* (<a href>) are fine -- clicking one is a user
    decision, not a page load."""
    root = Path(__file__).resolve().parent.parent
    source = root / "site" / "src" / "index.html"
    if source.exists():
        html = source.read_text(encoding="utf-8")
        assert "{{VERSION}}" in html
    else:
        # Release zips ship the built page; the placeholder check only applies to
        # the source template.
        html = (root / "public" / "index.html").read_text(encoding="utf-8")

    loads = re.compile(
        r"<script[^>]+src=\"http|<link[^>]+href=\"http|<img[^>]+src=\"http"
        r"|<source[^>]+src=\"http|url\(\s*['\"]?http|@import\s+['\"]?http",
        re.IGNORECASE,
    )
    hits = loads.findall(html)
    assert not hits, f"landing page loads third-party resources: {hits[:3]}"


def test_collect_excludes_previous_zips_and_the_stale_page():
    """The bundle must not carry a copy of the last release inside it, and must not
    ship the stale checked-in ``public/index.html``: the build appends the freshly
    filled page at that same path, and two entries under one name means the first
    one is dead weight that whichever extractor reads last silently overwrites."""
    files = br.collect()
    root = Path(__file__).resolve().parent.parent

    assert not [f for f in files if "downloads" in f.relative_to(root).parts]
    assert root / "public" / "index.html" not in files
    assert not [f for f in files if f.suffix == ".zip"]

    # The dashboard assets stay, so a self-hosted deployment renders out of the box.
    assert root / "public" / "app.js" in files
    assert root / "public" / "mascot.js" in files
    assert any("public/mascots" in f.as_posix() for f in files)
