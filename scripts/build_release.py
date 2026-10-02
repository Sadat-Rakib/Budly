#!/usr/bin/env python3
"""Build the downloadable release for the showcase page.

Outputs (default: site/dist):
  index.html                          site/src/index.html with
                                      {{VERSION}}/{{SHA256}}/{{SIZE}} filled
  downloads/<slug>-<version>.zip      the self-host bundle (folder prefix inside the zip)
  downloads/<slug>-latest.zip         same file, stable URL for the download button
  downloads/SHA256SUMS.txt
  LICENSE.txt                         MIT text (contains BOTH copyright lines)

Fails the build if the bundle contains anything that looks like a secret.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import re
import shutil
import sys
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "node_modules",
    ".vercel",
    "site",
    "dist",
    ".v2c",
    ".kilo",
    ".qodo",
}
EXCLUDE_GLOBS = ["*.pyc", "*.pyo", ".DS_Store", ".env", ".env.*", "vercel.env", "*.log"]
KEEP_FILES = {".env.example"}

SECRET_PATTERNS = {
    "slack webhook": re.compile(
        r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]{16,}"
    ),
    "slack token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    "telegram bot token": re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),
    "openrouter key": re.compile(r"\bsk-or-v1-[A-Za-z0-9]{20,}"),
    "canvas token": re.compile(r"\b\d{3,6}~[A-Za-z0-9]{40,}\b"),
    "db url with password": re.compile(
        r"postgres(?:ql)?(?:\+asyncpg)?://[^:\s/@]+:(?!<|\[|\$|\{|your|PASSWORD|password|xxx|\*)[^@\s]{6,}@"
    ),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}


def included(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in EXCLUDE_DIRS for part in rel.parts[:-1]):
        return False
    if rel.name in KEEP_FILES:
        return True
    return not any(fnmatch.fnmatch(rel.name, g) for g in EXCLUDE_GLOBS)


def collect(exclude_root: Path | None = None) -> list[Path]:
    """Every bundled file, minus the release's own downloads directory.

    Only ``<out>/downloads`` is skipped (the previous zips, which would otherwise be
    written inside the new zip and make the bundle grow by itself on every rebuild).
    The rest of ``public/`` -- the dashboard assets: mascots, fonts, app.js,
    mascot.js, the filled index.html -- is *wanted* in the bundle, so a
    self-hosted deployment serves a working dashboard out of the box.
    """
    skip_dir: Path | None = None
    if exclude_root is not None:
        skip_dir = exclude_root / "downloads"
    out: list[Path] = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or not included(p):
            continue
        if skip_dir is not None:
            try:
                p.relative_to(skip_dir)
            except ValueError:
                pass
            else:
                continue
        out.append(p)
    return sorted(out)


def scan(files: list[Path]) -> list[str]:
    hits = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for label, rx in SECRET_PATTERNS.items():
            if rx.search(text):
                hits.append(f"{f.relative_to(ROOT)}: looks like a {label}")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", default="budly")
    ap.add_argument("--out", default=str(ROOT / "site" / "dist"))
    args = ap.parse_args()

    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    out = Path(args.out)
    dl = out / "downloads"
    shutil.rmtree(out, ignore_errors=True)
    dl.mkdir(parents=True)

    # The dashboard assets come from site/src (their canonical home); they are
    # copied into the output FIRST so the bundle collects a complete public/.
    for extra in (ROOT / "site" / "src").iterdir():
        if extra.name == "index.html":
            continue
        target = out / extra.name
        if extra.is_file():
            shutil.copy(extra, target)
        else:
            shutil.copytree(extra, target)

    files = collect(exclude_root=out.resolve())
    required = {"LICENSE", "README.md", "SETUP.md", ".env.example", "pyproject.toml"}
    missing = required - {str(f.relative_to(ROOT)) for f in files}
    if missing:
        print(f"ERROR: bundle is missing {sorted(missing)}", file=sys.stderr)
        return 1
    hits = scan(files)
    if hits:
        print("ERROR: possible secrets in bundle:\n  " + "\n  ".join(hits), file=sys.stderr)
        return 1

    prefix = f"{args.slug}-{version}"
    zip_path = dl / f"{prefix}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            info = zipfile.ZipInfo(
                f"{prefix}/{f.relative_to(ROOT)}", date_time=(2026, 1, 1, 0, 0, 0)
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if f.suffix in {".py", ".sh"} else 0o644) << 16
            z.writestr(info, f.read_bytes())

    # The zip ships a copy of the dashboard page with the version filled in. The
    # checksum is deliberately NOT embedded here: a page inside the zip cannot
    # contain that zip's own hash. It lives in SHA256SUMS.txt beside the download.
    zip_html = (ROOT / "site" / "src" / "index.html").read_text(encoding="utf-8")
    zip_html = zip_html.replace("{{VERSION}}", version)
    zip_html = zip_html.replace("{{SHA256}}", "see SHA256SUMS.txt beside the download")
    zip_html = zip_html.replace("{{SIZE}}", "free & open source")
    with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED) as z:
        info = zipfile.ZipInfo(f"{prefix}/public/index.html", date_time=(2026, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        z.writestr(info, zip_html.encode("utf-8"))

    sha = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    size_kb = round(zip_path.stat().st_size / 1024)
    shutil.copy(zip_path, dl / f"{args.slug}-latest.zip")
    (dl / "SHA256SUMS.txt").write_text(f"{sha}  {zip_path.name}\n")
    shutil.copy(ROOT / "LICENSE", out / "LICENSE.txt")

    # The showcase page gets the real checksum: it is written after the zip is
    # final, and it lives outside the zip, so there is no self-reference.
    src_html = ROOT / "site" / "src" / "index.html"
    html = src_html.read_text(encoding="utf-8")
    for key, val in {"VERSION": version, "SHA256": sha, "SIZE": f"{size_kb} KB"}.items():
        html = html.replace("{{" + key + "}}", val)
    (out / "index.html").write_text(html, encoding="utf-8")

    print(f"OK {zip_path.name} {size_kb} KB sha256={sha[:12]}… ({len(files) + 1} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
