"""Recognition of console prompts that stall a build until Enter is pressed."""

import re

# Build steps print these prompts and then wait for keyboard input. A lone Enter
# makes the tools skip the prompt (an empty answer means "skip"), so the runner
# can unblock the step automatically or when the user presses the Enter button.
PROMPT = re.compile(r"\benter (?:your username|password for|passphrase for)\b", re.IGNORECASE)


def ends_with_prompt(text):
    """True when the end of *text* is an input prompt waiting for a key press."""
    tail = text[-4096:].rstrip()
    if not tail:
        return False
    return bool(PROMPT.search(tail.rsplit("\n", 1)[-1].rstrip("\r")))
