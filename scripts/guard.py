"""Privacy, identity and secret checks for commits and pushes.

    python scripts/guard.py staged            check the staged files (pre-commit)
    python scripts/guard.py message <file>    check a commit message (commit-msg)
    python scripts/guard.py outgoing          check commits about to be pushed (pre-push)

The word list, extra local-only paths and the expected author identity are read from
.local/guard.json, which is never committed. If that file is missing, or its word list is empty,
every check fails. The guard fails closed, never open.

Paths and secret shapes that hold no personal data are built in, so they apply even before the
local file is filled in.
"""

import json
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

# Local-only paths, refused wherever they appear. An entry ending in "/" is a folder.
BUILT_IN_PATHS = [
    ".env",
    ".local/",
    "config/owners.yaml",
    "recordings/",
    "runs/",
    "dist/",
    "_trash/",
    # The local assistant notes file. Built from two halves so this file never contains the name
    # itself, which the local word list refuses.
    "CLA" + "UDE.md",
]

SECRETS = [
    re.compile(r"ATATT[0-9A-Za-z_\-=]{20,}"),  # Atlassian API token shape
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"^[ \t]*JIRA_API_TOKEN[ \t]*=[ \t]*\S+", re.MULTILINE),  # a filled-in token line
]

SIZE_LIMIT = 2 * 1024 * 1024
SIZE_EXCEPTIONS = {"docs/demo.gif": 10 * 1024 * 1024}

# An absolute path to a user folder reveals the machine's user and folder names.
# Matches the drive-letter form and the Git Bash form (a slash, one letter, a slash).
ABSOLUTE = re.compile(r"(?:\b[a-z]:[\\/]+|(?<![\w./])/[a-z]/)users[\\/]", re.IGNORECASE)
# A trailer line such as a co-author credit.
TRAILER = re.compile(r"^\s*[a-z-]+-by:", re.IGNORECASE | re.MULTILINE)


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, check=True)
    return result.stdout.decode("utf-8", errors="replace").strip()


def git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", *args], capture_output=True, check=True).stdout


def fail(problems: list[str]) -> None:
    print("\nguard: refused.\n" + "\n".join(f"  - {p}" for p in problems) + "\n", file=sys.stderr)
    sys.exit(1)


def load_config(root: Path) -> dict:
    config = root / ".local" / "guard.json"
    if not config.exists():
        fail([".local/guard.json is missing, so nothing can be checked. Refusing (fail closed)."])
    try:
        cfg = json.loads(config.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        fail([".local/guard.json is not valid JSON. Refusing (fail closed)."])
    if not [w for w in cfg.get("forbiddenWords", []) if w]:
        fail([".local/guard.json has an empty forbiddenWords list. Refusing (fail closed)."])
    return cfg


def word_matcher(word: str) -> Callable[[str], bool]:
    # Short entries (4 characters or fewer) match only as whole words, so a run of letters
    # inside a hash cannot trip them. Longer entries match anywhere, including inside other words.
    word = word.lower()
    if len(word) <= 4:
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])")
        return lambda text: pattern.search(text.lower()) is not None
    return lambda text: word in text.lower()


def scan_text(label: str, text: str, matchers: list[Callable[[str], bool]]) -> list[str]:
    problems = [
        f"{label} contains a forbidden word (entry {i + 1} in guard.json)"
        for i, matches in enumerate(matchers)
        if matches(text)
    ]
    if ABSOLUTE.search(text):
        problems.append(f"{label} contains an absolute path to a user folder")
    return problems


def _matches_built_in(path: str, entry: str) -> bool:
    path, entry = path.lower(), entry.lower()
    if entry.endswith("/"):
        return path.startswith(entry) or f"/{entry}" in path
    return path == entry or path.endswith(f"/{entry}")


def _matches_extra(path: str, entry: str) -> bool:
    # The sibling project's rule, kept as is for the local list: broad on purpose.
    return path == entry or path.startswith(entry) or path.lower().endswith(entry.lower())


def is_forbidden_path(path: str, extra: list[str]) -> bool:
    return any(_matches_built_in(path, p) for p in BUILT_IN_PATHS) or any(
        _matches_extra(path, p) for p in extra if p
    )


def has_trailer(message: str) -> bool:
    return TRAILER.search(message) is not None


def find_secrets(label: str, text: str) -> list[str]:
    return [
        f"{label} contains something shaped like a secret (pattern {i + 1})"
        for i, pattern in enumerate(SECRETS)
        if pattern.search(text)
    ]


def size_limit(path: str) -> int:
    return SIZE_EXCEPTIONS.get(path, SIZE_LIMIT)


def check_identity(cfg: dict) -> list[str]:
    problems = []
    if cfg.get("expectedEmail") and git("config", "user.email") != cfg["expectedEmail"]:
        problems.append("the commit author email is not the expected personal identity")
    if cfg.get("expectedName") and git("config", "user.name") != cfg["expectedName"]:
        problems.append("the commit author name is not the expected personal identity")
    return problems


def check_staged(cfg: dict, matchers: list, extra: list[str]) -> None:
    problems = check_identity(cfg)
    staged = git("diff", "--cached", "--name-only", "--diff-filter=ACMR")
    files = [f for f in staged.splitlines() if f]
    for f in files:
        if is_forbidden_path(f, extra):
            problems.append(f"{f} is a local-only file")
        problems += scan_text(f"the file name {f}", f, matchers)
        blob = git_bytes("show", f":{f}")
        if len(blob) > size_limit(f):
            problems.append(f"{f} is larger than {size_limit(f) // (1024 * 1024)} MB")
        if b"\x00" in blob:
            continue  # binary file: path and size checks only
        text = blob.decode("utf-8", errors="replace")
        problems += scan_text(f, text, matchers)
        problems += find_secrets(f, text)
    if problems:
        fail(problems)
    print(f"guard: {len(files)} staged file(s) checked, clean.")


def check_message(path: str, matchers: list) -> None:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    text = "\n".join(line for line in lines if not line.startswith("#"))
    problems = scan_text("the commit message", text, matchers)
    problems += find_secrets("the commit message", text)
    if has_trailer(text):
        problems.append("the commit message has a trailer line (for example a co-author line)")
    if problems:
        fail(problems)


def check_outgoing(cfg: dict, matchers: list, extra: list[str]) -> None:
    problems = []
    for line in sys.stdin.read().strip().splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local_sha, remote_sha = parts[1], parts[3]
        if re.fullmatch(r"0+", local_sha):
            continue  # deleting a ref: nothing to scan
        new = re.fullmatch(r"0+", remote_sha) is not None
        commit_range = local_sha if new else f"{remote_sha}..{local_sha}"
        for sha in git("rev-list", commit_range).splitlines():
            message = git("log", "-1", "--format=%B", sha)
            problems += scan_text(f"commit {sha[:7]} message", message, matchers)
            problems += find_secrets(f"commit {sha[:7]} message", message)
            if has_trailer(message):
                problems.append(f"commit {sha[:7]} has a trailer line")
            author = git("log", "-1", "--format=%ae", sha)
            if cfg.get("expectedEmail") and author != cfg["expectedEmail"]:
                problems.append(f"commit {sha[:7]} has an unexpected author email")
        for f in git("ls-tree", "-r", "--name-only", local_sha).splitlines():
            if is_forbidden_path(f, extra):
                problems.append(f"{f} is a local-only file")
    if problems:
        fail(problems)
    print("guard: outgoing commits checked, clean.")


def main() -> None:
    cfg = load_config(Path(git("rev-parse", "--show-toplevel")))
    matchers = [word_matcher(str(w)) for w in cfg.get("forbiddenWords", []) if w]
    extra = [str(p) for p in cfg.get("forbiddenPaths", []) if p]

    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "staged":
        check_staged(cfg, matchers, extra)
    elif mode == "message" and len(sys.argv) > 2:
        check_message(sys.argv[2], matchers)
    elif mode == "outgoing":
        check_outgoing(cfg, matchers, extra)
    else:
        fail([f'unknown mode "{mode}": use staged, message <file>, or outgoing'])


if __name__ == "__main__":
    main()
