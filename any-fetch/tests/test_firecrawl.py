"""The Firecrawl scrape adapter: the request it sends, and how it reads Firecrawl's recorded answers."""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from any_fetch import AnyFetch, FetchedPage, MissingCredentialError, ProviderError
from any_fetch._api import DEFAULT_TIMEOUT

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "firecrawl"
KEY = "00000000-0000-0000-0000-000000000000"
BASE = "http://firecrawl:3002"
URL = "https://example.com/"


@pytest.fixture(autouse=True)
def _default_base(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep a base URL set in the developer's environment out of the requests checked here."""
    monkeypatch.delenv("FIRECRAWL_API_BASE", raising=False)


def _recorded(case: str) -> httpx.Response:
    recorded = json.loads((FIXTURES / f"{case}.json").read_text())
    body = recorded["body"]
    if isinstance(body, str):
        return httpx.Response(recorded["status"], text=body)
    return httpx.Response(recorded["status"], json=body)


async def _fetch(
    answer: httpx.Response | None = None, *, api_base: str | None = BASE, **kwargs: Any
) -> tuple[FetchedPage, httpx.Request]:
    """Fetch through a transport that answers with ``answer`` and keeps the request it was sent."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return answer if answer is not None else _recorded("normal")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with AnyFetch.create("firecrawl", api_key=KEY, api_base=api_base, client=client) as firecrawl:
            page = await firecrawl.fetch(URL, **kwargs)
    return page, sent[0]


def _payload(request: httpx.Request) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(request.content)
    return payload


SENTINEL = "sentinel-body-text"


def _answer(**data: Any) -> dict[str, Any]:
    """A successful answer with every field the adapter reads, and a sentinel no error may carry."""
    return {
        "success": True,
        "data": {
            "markdown": f"page {SENTINEL}",
            "metadata": {"title": f"Title {SENTINEL}", "sourceURL": URL, "statusCode": 200},
            **data,
        },
    }


def test_metadata() -> None:
    metadata = AnyFetch.get_provider_metadata("firecrawl")
    assert (metadata.tier, metadata.env_key, metadata.requires_api_key) == ("production", "FIRECRAWL_API_KEY", True)
    assert (metadata.max_urls_per_call, metadata.formats) == (1, ["markdown"])
    # The one provider here that both renders scripts and can be self-hosted.
    assert metadata.renders_javascript
    assert (metadata.requires_api_base, metadata.default_api_base) == (True, None)
    assert not metadata.reports_final_url


@pytest.mark.asyncio
async def test_sends_the_url_with_the_key_in_a_bearer_header_and_asks_only_for_markdown() -> None:
    _, request = await _fetch()
    assert (request.method, str(request.url)) == ("POST", f"{BASE}/v1/scrape")
    assert request.headers["authorization"] == f"Bearer {KEY}"
    payload = _payload(request)
    assert payload["url"] == URL
    # Firecrawl's own default is markdown AND rawHtml; the second is never read.
    assert payload["formats"] == ["markdown"]


@pytest.mark.asyncio
async def test_reads_the_text_the_title_and_the_date_from_a_successful_answer() -> None:
    answer = _answer(metadata={"title": "A title", "sourceURL": URL, "publishedTime": "2026-03-04T05:06:07Z"})
    page, _ = await _fetch(httpx.Response(200, json=answer))
    assert (page.text, page.title) == (f"page {SENTINEL}", "A title")
    assert page.published is not None and page.published.year == 2026
    assert page.cost_source == "none"


@pytest.mark.asyncio
async def test_reports_the_url_as_asked_because_firecrawl_does_not_report_redirects() -> None:
    # `metadata.sourceURL` is assigned before the fetch, so it cannot be the final address.
    answer = _answer(metadata={"title": "", "sourceURL": "https://elsewhere.example/"})
    page, _ = await _fetch(httpx.Response(200, json=answer))
    assert (page.url, page.final_url) == (URL, URL)


@pytest.mark.asyncio
async def test_a_page_firecrawl_could_not_extract_is_empty_text_rather_than_an_error() -> None:
    page, _ = await _fetch(httpx.Response(200, json={"success": True, "data": {"metadata": {}}}))
    assert (page.text, page.title, page.error) == ("", "", None)


@pytest.mark.asyncio
async def test_cuts_the_text_at_max_chars_and_says_so() -> None:
    page, _ = await _fetch(max_chars=4)
    assert (page.text, page.text_truncated) == ("page", True)


@pytest.mark.asyncio
async def test_asks_firecrawl_for_less_time_than_the_client_will_wait() -> None:
    """Asking for more would have the client cut the connection first, losing the answer."""
    _, request = await _fetch()
    asked_ms = _payload(request)["timeout"]
    assert 0 < asked_ms < DEFAULT_TIMEOUT * 1000


@pytest.mark.asyncio
async def test_passes_a_native_option_through_by_name() -> None:
    # Firecrawl rejects an unknown key outright, so options are declared and forwarded one by one.
    _, request = await _fetch(waitFor=2000)
    assert _payload(request)["waitFor"] == 2000


@pytest.mark.asyncio
async def test_an_error_status_is_an_http_error_that_never_carries_the_body() -> None:
    with pytest.raises(ProviderError) as raised:
        await _fetch(_recorded("error"))
    assert raised.value.tag == "http_error"
    assert SENTINEL not in str(raised.value)


@pytest.mark.asyncio
async def test_a_success_false_body_is_a_failure_even_with_a_200() -> None:
    with pytest.raises(ProviderError) as raised:
        await _fetch(httpx.Response(200, json={"success": False, "error": f"refused {SENTINEL}"}))
    assert raised.value.tag == "http_error"
    assert SENTINEL not in str(raised.value)


@pytest.mark.asyncio
async def test_a_field_of_the_wrong_type_fails_rather_than_degrading() -> None:
    with pytest.raises(ProviderError) as raised:
        await _fetch(httpx.Response(200, json={"success": True, "data": {"markdown": 42}}))
    assert raised.value.tag == "unexpected_response"


@pytest.mark.asyncio
async def test_an_unreadable_published_time_is_dropped_rather_than_failing() -> None:
    # The value is the page author's `article:published_time`, so it is whatever they wrote.
    answer = _answer(metadata={"title": "", "publishedTime": "D:20240410211143Z"})
    page, _ = await _fetch(httpx.Response(200, json=answer))
    assert page.published is None


@pytest.mark.asyncio
async def test_without_a_key_it_refuses_before_sending_anything() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
        with pytest.raises(MissingCredentialError):
            async with AnyFetch.create("firecrawl", api_key=None, api_base=BASE, client=client):
                pass
