"""Debug helper for role pattern."""
import re

p = re.compile(
    r"\bi\s+work\s+as\s+(?:a|an|the)\s+(?P<val>[a-zA-Z][a-zA-Z /-]{2,40}?)(?=[,.!?]|\s+(?:at|in|on|with|for|and|but|where)\b|$)"
    r"|\bi'?m\s+(?:a|an|the)\s+(?P<val2>[a-zA-Z][a-zA-Z /-]{2,40}?)(?=[,.!?]|\s+(?:at|in|on|with|for|and|but|where)\b|$)"
    r"|\bi\s+am\s+(?:a|an|the)\s+(?P<val3>[a-zA-Z][a-zA-Z /-]{2,40}?)(?=[,.!?]|\s+(?:at|in|on|with|for|and|but|where)\b|$)",
    re.IGNORECASE,
)

tests = [
    "I am a software engineer.",
    "I'm an ML engineer.",
    "I'm a data scientist.",
    "I work as a developer.",
    "I'm a backend engineer at Google.",
    "I am an ML engineer.",
    "I work as a backend engineer.",
]
for t in tests:
    m = p.search(t)
    val = None
    if m:
        val = m.group("val") or m.group("val2") or m.group("val3")
    print(f"{t!r:48s} -> {val}")
