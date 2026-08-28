"""Debug helper — print the works_at regex matches on test phrases."""
import re

ORG = r"(?:[A-Z][\w&.-]+(?:\s+[A-Z][\w&.-]+)*)"
WORK_AT = rf"(?P<val>{ORG})"
p = re.compile(
    rf"\bi\s+(?:(?:now|currently|these days)\s+)?(?:work|worked|working|'m working|am working)\s+(?:at|for)\s+(?:the\s+)?{WORK_AT}",
    re.IGNORECASE,
)

tests = [
    "I work at Stripe.",
    "I'm now working at OpenAI.",
    "I am now working at OpenAI.",
    "I now work at OpenAI.",
    "I currently work at OpenAI.",
    "I am working at OpenAI.",
    "I'm working at OpenAI.",
    "I'm at OpenAI now.",
]
for t in tests:
    m = p.search(t)
    val = m.group("val") if m else None
    print(f"{t!r:42s} -> {val}")
