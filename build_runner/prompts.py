"""Recognition of console prompts that stall a build until Enter is pressed."""

import re

# Build steps print these prompts and then wait for keyboard input. A lone Enter
# makes the tools skip the prompt (an empty answer means "skip"), so the runner
# can unblock the step automatically or when the user presses the Enter button.
PROMPT = re.compile(r"\benter (?:your username|password for|passphrase for)\b", re.IGNORECASE)


def ends_with_prompt(text):
    """True when the end of *text* is an input prompt waiting for a key press."""
    # Scan back over blank lines and this tool's own notes: a note can land on
    # top of the step's newest prompt (both append to the same log), and a
    # prompt can also follow every note once the step re-asks.
    for line in reversed(text[-4096:].splitlines()):
        line = line.strip()
        if not line or line.startswith("[Build Runner]"):
            continue
        return bool(PROMPT.search(line))
    return False
