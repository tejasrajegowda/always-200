"""The one way changes get committed to this repo.

    python scripts/commit.py "Plain one-line description of what changed"

1. checks the message: one line, at most 100 characters
2. runs the same checks as CI: ruff lint, ruff format check, the tests
3. stages everything the ignore rules allow
4. runs the privacy guard on the message and on the staged files
5. commits (the git hooks run the same guard again)

It never pushes. Pushing is done by hand, after reviewing what was committed.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

MAX_LENGTH = 100
CHECKS = [
    ("uv", "run", "ruff", "check", "."),
    ("uv", "run", "ruff", "format", "--check", "."),
    ("uv", "run", "pytest", "-q"),
]


def run(*cmd: str) -> None:
    if subprocess.run(cmd).returncode != 0:
        print(f"\ncommit: stopped, {' '.join(cmd)} failed.", file=sys.stderr)
        sys.exit(1)


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout.strip()


def message_problem(message: str) -> str | None:
    if not message:
        return "the message is empty"
    if "\n" in message or "\r" in message:
        return "the message must be one line"
    if len(message) > MAX_LENGTH:
        return f"the message is {len(message)} characters; the limit is {MAX_LENGTH}"
    return None


def main() -> None:
    message = " ".join(sys.argv[1:]).strip()
    problem = message_problem(message)
    if problem:
        print(f"commit: {problem}.", file=sys.stderr)
        print('usage: python scripts/commit.py "what changed"', file=sys.stderr)
        sys.exit(2)

    root = Path(git("rev-parse", "--show-toplevel"))
    os.chdir(root)
    guard = str(Path("scripts") / "guard.py")

    for check in CHECKS:
        run(*check)

    run("git", "add", "-A")
    if not git("diff", "--cached", "--name-only"):
        print("commit: nothing to commit.")
        return
    # Windows does not record the executable bit; set it so the hooks run on any clone.
    hooks = [p.as_posix() for p in sorted(Path(".githooks").glob("*")) if p.is_file()]
    if hooks:
        run("git", "update-index", "--chmod=+x", *hooks)

    with tempfile.TemporaryDirectory() as tmp:
        msg_file = Path(tmp) / "MSG"
        msg_file.write_text(message + "\n", encoding="utf-8")
        run(sys.executable, guard, "message", str(msg_file))
        run(sys.executable, guard, "staged")
        run("git", "commit", "--file", str(msg_file))

    print("\ncommit: done. Nothing was pushed. Review it with:  git show --stat HEAD")
    print("Push it yourself from the repo folder with:          git push")


if __name__ == "__main__":
    main()
