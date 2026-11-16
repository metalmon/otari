"""Firecrawl scrape: ``POST /v1/scrape``, with the key in a bearer header.

Firecrawl renders a page in a headless browser and returns its text as
markdown. It is the only provider here that both runs JavaScript and can be
self-hosted, which is what a closed deployment needs: a page that draws itself
with scripts comes back empty from ``builtin``, and the one other provider that
renders, Tabstack, is a hosted service.

Written against **Firecrawl Simple**
(https://github.com/devflowinc/firecrawl-simple), the community fork that keeps
the scrape API and drops the hosted extras — no LLM extraction, and a plain
Puppeteer service in place of the proprietary browser. The request and response
below are the ones both it and upstream Firecrawl answer, so the adapter does
not care which of the two a deployment runs. It has no hosted service and no
public endpoint, so ``api_base`` is required and there is no default.

``formats`` is pinned to ``["markdown"]``. Firecrawl defaults to markdown AND
raw HTML, which doubles the response for a field this adapter never reads.

Firecrawl validates its request body **strictly**: an unknown key is a 400, not
an ignored field. Native options are therefore declared one by one and passed
through by name, unlike adapters for APIs that ignore what they do not know.

A failed call is an HTTP status whose body can quote the URL, so only the
status is kept, tagged ``http_error``. A successful answer carries
``success: true`` and a ``data`` object; ``markdown`` is optional there,
because a page Firecrawl reached but could not extract returns an empty
document rather than failing, and that is reported as empty text, not an error.

``metadata.sourceURL`` is the URL as **asked**, assigned before the fetch
(``scraper/WebScraper/index.ts``), so it is not the address after redirects —
hence ``reports_final_url=False``. ``metadata.publishedTime`` comes from the
page's own ``article:published_time`` meta tag when it has one; it is parsed
leniently and dropped when it is not a date, since it is author-supplied.

Firecrawl reports no cost: a self-hosted deployment has none to report.

API: https://docs.firecrawl.dev/api-reference/endpoint/scrape.
"""

from datetime import datetime
from typing import Any

import httpx

from any_fetch._api import AnyFetch
from any_fetch._errors import ProviderError
from any_fetch._types import FetchedPage, OptionSpec, ProviderMetadata

# Firecrawl's own default is 30 s, which a browser render can exceed on a heavy page.
_DEFAULT_TIMEOUT_MS = 60_000


class FirecrawlProvider(AnyFetch):
    """Firecrawl's scrape endpoint, hosted or self-hosted."""

    METADATA = ProviderMetadata(
        name="firecrawl",
        doc_url="https://docs.firecrawl.dev/api-reference/endpoint/scrape",
        env_key="FIRECRAWL_API_KEY",
        env_api_base="FIRECRAWL_API_BASE",
        # Firecrawl Simple accepts any UUID as a bearer token and keeps no key
        # store, but the header is still required, and hosted Firecrawl checks it.
        requires_api_key=True,
        # Self-hosted only: there is no address to fall back to.
        requires_api_base=True,
        default_api_base=None,
        tier="production",
        max_urls_per_call=1,
        renders_javascript=True,
        reports_final_url=False,
        formats=["markdown"],
        options=[
            OptionSpec(
                name="waitFor",
                type="integer",
                default=0,
                description="Milliseconds to wait after load before reading the page, for content that arrives late.",
            ),
            OptionSpec(
                name="blockMedia",
                type="boolean",
                default=True,
                description="Skip images, fonts and media, which a text extraction never needs.",
            ),
            OptionSpec(
                name="onlyMainContent",
                type="boolean",
                description="Return the page's main content, dropping navigation, headers and footers.",
            ),
            OptionSpec(
                name="includeTags",
                type="array",
                description="CSS selectors to keep, dropping everything else.",
            ),
            OptionSpec(
                name="excludeTags",
                type="array",
                description="CSS selectors to drop from the page before extraction.",
            ),
            OptionSpec(
                name="headers",
                type="object",
                description="Request headers Firecrawl sends to the page, such as a cookie for a gated article.",
            ),
        ],
    )

    async def _fetch(self, url: str, *, max_chars: int | None, options: dict[str, Any]) -> FetchedPage:
        # Only declared options are forwarded: Firecrawl rejects an unknown key outright.
        payload: dict[str, Any] = {
            "url": url,
            "formats": ["markdown"],
            "timeout": _DEFAULT_TIMEOUT_MS,
            **options,
        }

        endpoint = f"{(self._api_base or '').rstrip('/')}/v1/scrape"
        response = await self._http.request(
            "POST", endpoint, json=payload, headers={"Authorization": f"Bearer {self._api_key or ''}"}
        )
        data = _data(response)
        text = data.get("markdown", "")
        if not isinstance(text, str):
            raise _unexpected(response)
        metadata = _metadata(data, response)
        return FetchedPage(
            url=url,
            # Firecrawl reports the URL as asked, so a redirect is not visible here.
            final_url=url,
            title=_title(metadata, response),
            text=text[:max_chars],
            # Firecrawl returns extracted markdown and does not say what the page was served as.
            content_type="",
            published=_published(metadata),
            cost_source="none",
            text_truncated=max_chars is not None and len(text) > max_chars,
            raw=data,
        )


def _data(response: httpx.Response) -> dict[str, Any]:
    """Return the ``data`` object of a successful answer, or raise what Firecrawl signaled.

    An error body is a sentence that can quote the URL, so only the status is kept.
    """
    if response.status_code >= 400:
        raise ProviderError(FirecrawlProvider.METADATA.name, response.status_code, "http_error")
    try:
        body = response.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        raise ProviderError(FirecrawlProvider.METADATA.name, response.status_code, "invalid_response")
    # A 200 carrying `success: false` is Firecrawl reporting a failure in the body.
    if body.get("success") is not True:
        raise ProviderError(FirecrawlProvider.METADATA.name, response.status_code, "http_error")
    data = body.get("data")
    if not isinstance(data, dict):
        raise _unexpected(response)
    return data


def _unexpected(response: httpx.Response) -> ProviderError:
    """A successful answer that lacks a field Firecrawl documents, or carries one of the wrong type."""
    return ProviderError(FirecrawlProvider.METADATA.name, response.status_code, "unexpected_response")


def _metadata(data: dict[str, Any], response: httpx.Response) -> dict[str, Any]:
    """The answer's ``metadata`` object, ``{}`` when Firecrawl sent none; it is optional."""
    if "metadata" not in data:
        return {}
    metadata = data["metadata"]
    if not isinstance(metadata, dict):
        raise _unexpected(response)
    return metadata


def _title(metadata: dict[str, Any], response: httpx.Response) -> str:
    """The page's title, ``""`` when Firecrawl found none; it is optional."""
    title = metadata.get("title", "")
    if not isinstance(title, str):
        raise _unexpected(response)
    return title


def _published(metadata: dict[str, Any]) -> datetime | None:
    """The page's own publication date, when it declared one this can be read.

    The value is the page's `article:published_time` meta tag, so it is whatever its
    author wrote. A value that is not a date is dropped rather than failing the call.
    """
    published = metadata.get("publishedTime")
    if not isinstance(published, str) or not published:
        return None
    try:
        return datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return None
