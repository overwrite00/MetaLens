"""Update the '*Last updated: YYYY-MM-DD*' footer of staged Markdown docs to today.

Invoked by the .githooks/pre-commit hook. Only touches files that are staged
for commit and already contain a footer line matching the expected pattern.
"""

import datetime
import re
import subprocess
import sys

FOOTER_PATTERN = re.compile(r"^(\*Last updated: )\d{4}-\d{2}-\d{2}(\*[ \t]*)$", re.MULTILINE)


def staged_markdown_files() -> list[str]:
    output = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in output.splitlines() if line.endswith(".md")]


def update_file(path: str, today: str) -> bool:
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    new_content, count = FOOTER_PATTERN.subn(rf"\g<1>{today}\g<2>", content)
    if count == 0 or new_content == content:
        return False

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)
    return True


def main() -> int:
    today = datetime.date.today().isoformat()
    changed = []

    for path in staged_markdown_files():
        try:
            if update_file(path, today):
                changed.append(path)
        except FileNotFoundError:
            continue

    if changed:
        subprocess.run(["git", "add", *changed], check=True)
        for path in changed:
            print(f"Updated 'Last updated' footer in {path} -> {today}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
