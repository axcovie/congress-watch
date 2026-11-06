"""Turn newly collected results into a daily, nonpartisan briefing."""
from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime
from pathlib import Path

from .store import Store
from .telem_client import TelemClient, TelemError
from .text import clean_snippet, lede_from_markdown


def _group(rows):
    groups: "OrderedDict[str, list]" = OrderedDict()
    for r in rows:
        groups.setdefault(r["topic_key"], []).append(r)
    return groups


def _headline_markdown(groups, labels, numbered, context_by_url) -> str:
    """Headlines grouped by section, each with whole sentences only.

    Prefers the article's opening sentences (from Telem Fetch); otherwise the complete
    sentences in the search snippet; otherwise just the headline. Never cuts mid-sentence.
    """
    lines = []
    for key, rows in groups.items():
        lines.append(f"## {labels.get(key, key)}")
        for r in rows:
            n = numbered[r["url"]]
            title = re.sub(r"\s+", " ", (r["title"] or r["url"])).strip()
            summ = lede_from_markdown(context_by_url.get(r["url"])) or clean_snippet(r["summary"])
            lines.append(f"- **{title}** [{n}]")
            if summ:
                lines.append(f"  {summ}")
        lines.append("")
    return "\n".join(lines)


def _fetch_context(client, groups, fetch_top) -> dict[str, str]:
    context: dict[str, str] = {}
    if not client or fetch_top <= 0:
        return context
    to_fetch = [r["url"] for rs in groups.values() for r in rs[:fetch_top]]
    try:
        for url, fr in client.fetch(to_fetch).items():
            if fr.get("status") == "succeeded" and fr.get("content"):
                context[url] = fr["content"]
    except TelemError as exc:
        print(f"warning: fetch failed, using snippets only ({exc})")
    return context


def build_brief(
    store: Store,
    client: TelemClient | None,
    labels: dict[str, str],
    out_dir: str | Path = "briefs",
    fetch_top: int = 3,
    mark: bool = True,
) -> Path | None:
    rows = store.unbriefed()
    if not rows:
        return None
    groups = _group(rows)

    numbered: dict[str, int] = {}
    for r in rows:
        numbered.setdefault(r["url"], len(numbered) + 1)

    # Fetch the article for items whose snippet has no complete sentence,
    # so they get a readable lede instead of a fragment.
    needs_page = OrderedDict(
        (k, [r for r in rs if not clean_snippet(r["summary"])]) for k, rs in groups.items()
    )
    context = _fetch_context(client, needs_page, fetch_top)
    body = _headline_markdown(groups, labels, numbered, context)

    by_num = {n: u for u, n in numbered.items()}
    titles = {r["url"]: (r["title"] or r["url"]) for r in rows}
    sources = "\n".join(f"{n}. [{titles[by_num[n]]}]({by_num[n]})" for n in sorted(by_num))

    stamp = datetime.now()
    md = (
        f"# Congress Watch — {stamp:%B %d, %Y}\n\n"
        f"_{len(rows)} new items from {len(groups)} monitors. Generated {stamp:%H:%M}._\n\n"
        f"{body.strip()}\n\n## Sources\n{sources}\n"
    )

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / f"{stamp:%Y-%m-%d_%H%M}.md"
    md_path.write_text(md)
    _write_html(md, md_path.with_suffix(".html"))
    if mark:
        store.mark_briefed([r["id"] for r in rows])
    return md_path


def _write_html(md: str, path: Path) -> None:
    try:
        import markdown

        body = markdown.markdown(md)
    except ImportError:  # pragma: no cover
        import html

        body = f"<pre>{html.escape(md)}</pre>"
    path.write_text(
        "<!DOCTYPE html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Congress Watch</title><style>"
        "body{font:16px/1.55 system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;color:#1d1d1f}"
        "h1{font-size:1.6rem}h2{font-size:1.15rem;margin-top:1.8rem;border-bottom:1px solid #ddd;padding-bottom:.2rem}"
        "a{color:#0b57d0}em{color:#666}li{margin:.3rem 0}"
        "@media(prefers-color-scheme:dark){body{background:#111;color:#eee}a{color:#8ab4f8}h2{border-color:#333}}"
        f"</style></head><body>{body}</body></html>"
    )
