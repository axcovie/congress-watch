"""Turn search snippets and fetched pages into complete, readable sentences."""
from __future__ import annotations

import re

_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
_LEADING_DATE = re.compile(
    rf"^\s*(?:{_MONTH}\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}\s+{_MONTH}\s+\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}}"
    r"|\d+\s+(?:hours?|days?|minutes?)\s+ago)\s*(?:[-—–·|:]|\.\.\.|…)?\s*",
    re.I,
)
# Split after . ! ? (optionally followed by a closing quote/paren) when the next
# sentence starts with a capital letter, digit, or opening quote.
_SENT_SPLIT = re.compile(r"(?<=[.!?])[\"'”’)\]]?\s+(?=[\"“‘(A-Z0-9])")
_ABBREV = re.compile(
    r"\b(?:Mr|Mrs|Ms|Dr|Sen|Rep|Gov|Gen|Lt|Col|Sgt|St|Jr|Sr|Inc|Co|Corp|No|vs|U\.S|D\.C|Jan|Feb|Mar|Apr"
    r"|Aug|Sept|Sep|Oct|Nov|Dec|R|D|I)\.$"
)
_BOILERPLATE = re.compile(
    r"(cookie|subscribe|sign up|sign in|log in|newsletter|advertis|all rights reserved|"
    r"javascript|your browser|click here|read more|share this|follow us|privacy policy|"
    r"terms of (use|service)|getty images|ap photo|reuters/|file photo)",
    re.I,
)


def _sentences(text: str) -> list[str]:
    parts = _SENT_SPLIT.split(text)
    out: list[str] = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        # Re-join pieces that were split after an abbreviation ("Sen. Smith").
        if out and _ABBREV.search(out[-1]):
            out[-1] = f"{out[-1]} {p}"
        else:
            out.append(p)
    return out


def _is_complete(s: str) -> bool:
    s = s.strip()
    return (
        len(s) >= 25
        and bool(re.match(r"[\"“‘(A-Z0-9]", s))
        and bool(re.search(r"[.!?][\"'”’)\]]?$", s))
        and not s.endswith("...")
        and "…" not in s[-3:]
        and not _BOILERPLATE.search(s)
    )


def _pick(sentences: list[str], max_chars: int, max_sentences: int) -> str:
    chosen: list[str] = []
    length = 0
    for s in sentences:
        if not _is_complete(s):
            continue
        if chosen and length + len(s) + 1 > max_chars:
            break
        chosen.append(s)
        length += len(s) + 1
        if len(chosen) >= max_sentences:
            break
    return " ".join(chosen)


def clean_snippet(text: str | None, max_chars: int = 420, max_sentences: int = 3) -> str:
    """Return only the whole sentences in a search snippet ('' if there are none).

    Snippets often look like "Nov 4, 2026 ... officials said the count ... The AP has
    not called the race." Fragments around the ellipses are dropped rather than cut.
    """
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    text = _LEADING_DATE.sub("", text)
    pieces = re.split(r"\s*(?:\.\.\.|…)\s*", text)
    sentences: list[str] = []
    for i, piece in enumerate(pieces):
        sents = _sentences(piece)
        # A piece that follows an ellipsis starts mid-sentence; one that precedes an
        # ellipsis ends mid-sentence. Drop those partial edges.
        if i > 0 and sents and not re.match(r"[\"“‘(A-Z0-9]", sents[0]):
            sents = sents[1:]
        sentences.extend(sents)
    return _pick(sentences, max_chars, max_sentences)


def lede_from_markdown(content: str | None, max_chars: int = 520, max_sentences: int = 3) -> str:
    """Opening sentences of an article from Telem Fetch's markdown, skipping nav,
    headings, captions, link lists, and boilerplate."""
    if not content:
        return ""
    paras: list[str] = []
    for block in re.split(r"\n\s*\n", content):
        b = block.strip()
        if not b or b.startswith(("#", ">", "|", "```", "![", "* ", "- ", "+ ")):
            continue
        b = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", b)            # images
        b = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", b)        # links -> text
        b = re.sub(r"[*_`]{1,3}", "", b)                       # emphasis
        b = re.sub(r"\s+", " ", b).strip()
        if len(b) < 80 or b.count(" ") < 12:
            continue                                           # bylines, captions, nav
        if _BOILERPLATE.search(b[:120]):
            continue
        paras.append(b)
        if sum(len(p) for p in paras) > 3000:
            break
    sentences: list[str] = []
    for p in paras:
        sentences.extend(_sentences(_LEADING_DATE.sub("", p)))
    return _pick(sentences, max_chars, max_sentences)
