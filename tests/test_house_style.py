"""House style for everything a reader sees: no em dashes.

Prose, docstrings, comments, strings the tool prints and the reports it writes
all use ordinary punctuation instead (a colon, a comma, a full stop or
parentheses). This test keeps it that way, including in files a generator
writes, because a rule nobody checks is a preference.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: Spelled as escapes, so this file does not trip its own check.
FORBIDDEN = {
    "\u2014": "em dash",
    "\u2013": "en dash",
    "&" + "mdash;": "HTML em dash",
    "&" + "ndash;": "HTML en dash",
}
TEXT_SUFFIXES = {".py", ".md", ".toml", ".json", ".html", ".yml", ".yaml", ".txt", ".cfg"}
SCANNED = ["src", "tests", "docs", "suites", ".github"]
TOP_LEVEL = ["README.md", "SECURITY.md", "LICENSE", "pyproject.toml", ".gitignore", ".env.example"]


def _files() -> list[Path]:
    found = [ROOT / name for name in TOP_LEVEL if (ROOT / name).is_file()]
    for directory in SCANNED:
        base = ROOT / directory
        if base.is_dir():
            found.extend(
                path
                for path in base.rglob("*")
                if path.is_file()
                and path.suffix in TEXT_SUFFIXES
                and "__pycache__" not in path.parts
            )
    return sorted(found)


def test_no_em_dashes_anywhere_a_reader_looks() -> None:
    offences: list[str] = []
    for path in _files():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for token, name in FORBIDDEN.items():
                if token in line:
                    offences.append(f"{path.relative_to(ROOT)}:{number}: {name}")
    assert not offences, "use a colon, comma, full stop or parentheses:\n" + "\n".join(offences)
