from typing import Any
from urllib.parse import urlparse

from revos.domain.untrusted import UntrustedText
from revos.mcp.registry import ToolContext, ToolSpec
from revos.mcp.scope import RunScope, ToolBlocked

PAGE_CHARS = 20_000


def _host_allowed(url: str, domain: str | None) -> bool:
    if not domain:
        return False
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    return parsed.scheme in {"http", "https"} and (host == domain or host.endswith("." + domain))


def search(ctx: ToolContext, scope: RunScope, args: dict[str, Any]) -> dict[str, Any]:
    hits = ctx.research.search(str(args["query"]), max_results=5)
    shown, held = [], 0
    for hit in hits:
        snippet = UntrustedText.wrap("web_page", hit.url, hit.snippet)
        if snippet.flagged:
            ctx.on_quarantine(snippet, scope.deal_id)
            held += 1
        else:
            shown.append({"title": hit.title, "url": hit.url, "snippet": snippet.for_model()})
    return {"results": shown, "held_for_review": held}


def fetch_site(ctx: ToolContext, scope: RunScope, args: dict[str, Any]) -> dict[str, Any]:
    url = str(args["url"])
    if not _host_allowed(url, scope.company_domain):
        raise ToolBlocked("url is outside the run's company website")
    page = ctx.research.fetch(url)
    if page is None:
        return {"url": url, "found": False}
    content = UntrustedText.wrap("web_page", url, page.content, is_html=page.is_html,
                                 max_chars=PAGE_CHARS)
    if content.flagged:
        ctx.on_quarantine(content, scope.deal_id)
        return {"url": url, "found": True, "content": None,
                "note": "this page contains text that tries to instruct the system; "
                        "it was withheld and a person will review it"}
    return {"url": url, "found": True, "content": content.for_model()}


SPECS = [
    ToolSpec("research", "search", "Search the web about this run's company.",
             {"query": {"type": "string"}}, ("query",), search),
    ToolSpec("research", "fetch_site", "Fetch a page from this run's company website.",
             {"url": {"type": "string"}}, ("url",), fetch_site),
]
