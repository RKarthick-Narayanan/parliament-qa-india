"""Case-insensitive exact word/phrase matching with source-safe normalization."""
import html
import re


def plain(value):
    text = str(value or "")
    for _ in range(2):
        text = html.unescape(text)
    text = re.sub(r"<\s*(?:br\s*/?|/p)\s*>", "\n", text, flags=re.I)
    return re.sub(r"<[^>]*>", " ", text)


def normalize(value):
    return re.sub(r"\s+", " ", re.sub(r"(?<=\w)-\s*\n\s*(?=\w)", "", plain(value))).casefold().strip()


def matches(value, term):
    text = normalize(value)
    pattern = r"(?<!\w)" + r"\s+".join(re.escape(x) for x in normalize(term).split()) + r"(?!\w)"
    hits = list(re.finditer(pattern, text))
    return len(hits), text[max(0, hits[0].start()-100):hits[0].end()+180] if hits else ""
