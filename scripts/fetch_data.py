#!/usr/bin/env python3
"""Bounded project-data downloader (safe by construction).

Security properties — this script can ONLY:
  * fetch URLs whose prefix is in ALLOWED_PREFIXES (hardcoded), and
  * write files under the repo's ``data/`` directory.
Anything else is refused. It uses certifi's CA bundle (not the macOS Keychain),
so it runs under the Claude Code sandbox without keychain access — provided the
host is in ``sandbox.network.allowedDomains``.

Usage:
    python scripts/fetch_data.py [set]        # default set: "openings"
"""

from __future__ import annotations

import ssl
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "data"

# The ONLY hosts/paths this script may ever download from.
ALLOWED_PREFIXES = (
    "https://raw.githubusercontent.com/lichess-org/chess-openings/master/",
)

# Named download sets: {repo-relative dest path: url}.
OPENINGS = {
    f"data/openings/{c}.tsv":
        f"https://raw.githubusercontent.com/lichess-org/chess-openings/master/{c}.tsv"
    for c in "abcde"
}

SETS: dict[str, dict[str, str]] = {"openings": OPENINGS}


def _ssl_context() -> ssl.SSLContext:
    """TLS context using certifi's bundle — avoids the macOS Keychain."""
    try:
        import certifi
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_verify_locations(certifi.where())
        return ctx
    except ImportError:
        return ssl.create_default_context()


def _fetch(rel_path: str, url: str) -> None:
    if not url.startswith(ALLOWED_PREFIXES):
        raise SystemExit(f"REFUSED (url not in allowlist): {url}")
    dest = (REPO / rel_path).resolve()
    if DATA != dest.parent and DATA not in dest.parents:
        raise SystemExit(f"REFUSED (writes only under data/): {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {url}")
    with urllib.request.urlopen(url, context=_ssl_context(), timeout=30) as resp:
        data = resp.read()
    dest.write_bytes(data)
    print(f"    -> {rel_path} ({len(data)} bytes)")


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "openings"
    chosen = SETS.get(which)
    if chosen is None:
        raise SystemExit(f"unknown set '{which}'; choices: {', '.join(SETS)}")
    print(f"fetching set '{which}' ({len(chosen)} files)…")
    for rel, url in chosen.items():
        _fetch(rel, url)
    print("done.")


if __name__ == "__main__":
    main()
