"""Unit tests for `backend/clients/wiki_client.py`.

All non-`live` tests run fully offline: `respx` intercepts every outbound
`httpx` call (assert_all_mocked=True by default), so an accidental real
network call fails the test loudly instead of silently reaching the
internet. Fixtures under `tests/fixtures/wiki_*.json` are real, unmodified
responses captured from the live API on 2026-08-14 (see
`backend/clients/wiki_client.py`'s module docstring for what was observed).

The one `@pytest.mark.live` test at the bottom is excluded from the default
`pytest` run (see `pyproject.toml`) and genuinely hits the real API.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from backend.clients.wiki_client import (
    WikiApiClientError,
    WikiApiTransientError,
    WikiClient,
    WikiPriceEntry,
)
from backend.config import Settings

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"
BASE_URL = "https://api.star-citizen.wiki/api"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _settings(**overrides) -> Settings:
    defaults: dict = dict(
        wiki_api_base_url=BASE_URL,
        http_timeout_seconds=1.0,
        http_max_retries=2,
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Replace tenacity's `time.sleep` with a recorder instead of a no-op.

    Retry tests run instantly (no multi-second real waits) while still
    letting assertions check *what* delay was requested -- e.g. that a 429's
    `Retry-After` header, not exponential backoff, drove the wait.
    """
    calls: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda seconds: calls.append(seconds))
    return calls


# --- list pagination ---------------------------------------------------


@respx.mock
def test_list_all_commodities_follows_pagination_to_completion():
    # Page 1 is the real, verbatim captured fixture (30 items, and a real
    # `links.next` pointing at page 2). Pages 2-3 are small, schema-accurate
    # synthetic continuations (only one real page was captured -- see
    # `wiki_client.py`'s docstring) so the loop is exercised across three
    # hops through to a `links.next: null` terminator.
    page1 = _load_fixture("wiki_commodities_list_page1.json")
    page2 = {
        "data": [
            {"uuid": "uuid-x", "key": "Xylite", "name": "Xylite", "slug": "xylite"},
            {"uuid": "uuid-y", "key": "Yttrium", "name": "Yttrium", "slug": "yttrium"},
        ],
        "links": {"next": f"{BASE_URL}/commodities?page%5Bnumber%5D=3"},
        "meta": {"current_page": 2, "last_page": 3},
    }
    page3 = {
        "data": [{"uuid": "uuid-z", "key": "Zeta", "name": "Zeta", "slug": "zeta"}],
        "links": {"next": None},
        "meta": {"current_page": 3, "last_page": 3},
    }
    route = respx.get(f"{BASE_URL}/commodities").mock(
        side_effect=[
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
            httpx.Response(200, json=page3),
        ]
    )

    client = WikiClient(settings=_settings())
    try:
        items = client.list_all_commodities(page_size=200)
    finally:
        client.close()

    assert route.call_count == 3
    assert len(items) == 30 + 2 + 1
    # First item comes from the real page-1 fixture, verbatim.
    assert items[0].slug == "ace-interceptor-helmet"
    assert items[0].uuid == "7abd6c08-3755-4a2a-ba3f-cb3019c2f213"
    # Last item comes from the synthetic terminating page.
    assert items[-1].slug == "zeta"


@respx.mock
def test_iter_commodities_stops_when_links_next_is_null():
    page1 = {
        "data": [{"uuid": "uuid-a", "key": "A", "name": "A", "slug": "a"}],
        "links": {"next": None},
        "meta": {"current_page": 1, "last_page": 1},
    }
    route = respx.get(f"{BASE_URL}/commodities").mock(return_value=httpx.Response(200, json=page1))

    client = WikiClient(settings=_settings())
    try:
        items = list(client.iter_commodities())
    finally:
        client.close()

    assert route.call_count == 1
    assert len(items) == 1


# --- detail parsing, using real captured fixtures -----------------------


@respx.mock
def test_get_commodity_detail_parses_real_laranite_fixture():
    raw = _load_fixture("wiki_commodity_laranite.json")
    respx.get(f"{BASE_URL}/commodities/laranite").mock(return_value=httpx.Response(200, json=raw))

    client = WikiClient(settings=_settings())
    try:
        detail = client.get_commodity_detail("laranite")
    finally:
        client.close()

    assert detail.slug == "laranite"
    assert detail.name == "Laranite"
    assert len(detail.purchase_entries) == 26

    entry = detail.purchase_entries[0]
    assert entry.terminal_name == "Admin - Stanton Gateway (Nyx)"
    assert entry.terminal_id == 802
    # Known-zero (can't buy here) is a real float, not None.
    assert entry.price_buy == 0.0
    assert entry.price_sell == 8500.0
    # `price_rent` is `null` on every real entry -- must parse to None, not 0.0.
    assert entry.price_rent is None
    assert entry.starmap_location is not None
    assert entry.starmap_location.star_system_name == "Nyx"
    assert entry.starmap_location.slug == "stanton-gateway"


@respx.mock
def test_get_commodity_detail_parses_real_agricium_fixture():
    raw = _load_fixture("wiki_commodity_agricium.json")
    respx.get(f"{BASE_URL}/commodities/agricium").mock(return_value=httpx.Response(200, json=raw))

    client = WikiClient(settings=_settings())
    try:
        detail = client.get_commodity_detail("agricium")
    finally:
        client.close()

    assert detail.slug == "agricium"
    assert len(detail.purchase_entries) == 33
    entry = detail.purchase_entries[0]
    assert entry.terminal_name == "Levski"
    assert entry.price_buy == 8379.0
    assert entry.price_sell == 0.0


@respx.mock
def test_get_commodity_detail_tolerates_sparse_prices():
    """`inert-materials` has exactly one known terminal price -- the real
    "mostly no price data at all" shape (every other terminal simply has no
    entry in `purchase[]`, i.e. missing, not a zero)."""
    raw = _load_fixture("wiki_commodity_inert_materials_sparse.json")
    respx.get(f"{BASE_URL}/commodities/inert-materials").mock(
        return_value=httpx.Response(200, json=raw)
    )

    client = WikiClient(settings=_settings())
    try:
        detail = client.get_commodity_detail("inert-materials")
    finally:
        client.close()

    assert detail.slug == "inert-materials"
    assert len(detail.purchase_entries) == 1
    entry = detail.purchase_entries[0]
    assert entry.price_buy == 0.0
    assert entry.price_sell == 4.0
    assert entry.price_rent is None


def test_price_entry_never_coerces_missing_price_to_zero():
    """Direct model-contract check, independent of what the live API
    happened to send: `price_buy`/`price_sell` must stay `None` when absent
    or explicitly `null`, per CLAUDE.md's edge-weight formula (missing !=
    0)."""
    entry = WikiPriceEntry.model_validate(
        {
            "price_buy": None,
            "price_sell": 42.0,
            "terminal_id": 1,
            "terminal_name": "Test Terminal",
        }
    )
    assert entry.price_buy is None
    assert entry.price_sell == 42.0

    entry_key_absent = WikiPriceEntry.model_validate(
        {
            "price_sell": 10.0,
            "terminal_id": 2,
            "terminal_name": "Another Terminal",
        }
    )
    assert entry_key_absent.price_buy is None
    assert entry_key_absent.price_sell == 10.0


# --- retry / backoff / error handling -----------------------------------


@respx.mock
def test_retry_then_success_on_transient_500(no_real_sleep):
    raw = _load_fixture("wiki_commodity_laranite.json")
    route = respx.get(f"{BASE_URL}/commodities/laranite").mock(
        side_effect=[
            httpx.Response(500, json={"message": "server error"}),
            httpx.Response(200, json=raw),
        ]
    )

    client = WikiClient(settings=_settings(http_max_retries=2))
    try:
        detail = client.get_commodity_detail("laranite")
    finally:
        client.close()

    assert detail.slug == "laranite"
    assert route.call_count == 2
    assert len(no_real_sleep) == 1  # exactly one backoff wait between attempts


@respx.mock
def test_gives_up_after_max_retries_on_persistent_failure(no_real_sleep):
    route = respx.get(f"{BASE_URL}/commodities/laranite").mock(
        return_value=httpx.Response(500, json={"message": "server error"})
    )

    client = WikiClient(settings=_settings(http_max_retries=2))
    try:
        with pytest.raises(WikiApiTransientError):
            client.get_commodity_detail("laranite")
    finally:
        client.close()

    # http_max_retries=2 -> 1 initial attempt + 2 retries = 3 total calls.
    assert route.call_count == 3
    assert len(no_real_sleep) == 2


@respx.mock
def test_429_respects_retry_after_header_over_backoff(no_real_sleep):
    raw = _load_fixture("wiki_commodity_laranite.json")
    route = respx.get(f"{BASE_URL}/commodities/laranite").mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}, json={"message": "slow down"}),
            httpx.Response(200, json=raw),
        ]
    )

    client = WikiClient(settings=_settings(http_max_retries=2))
    try:
        detail = client.get_commodity_detail("laranite")
    finally:
        client.close()

    assert detail.slug == "laranite"
    assert route.call_count == 2
    # The 429's Retry-After (2s) must drive the wait, not exponential backoff.
    assert no_real_sleep == [2.0]


@respx.mock
def test_connection_error_is_retried(no_real_sleep):
    raw = _load_fixture("wiki_commodity_laranite.json")
    route = respx.get(f"{BASE_URL}/commodities/laranite").mock(
        side_effect=[
            httpx.ConnectError("connection refused"),
            httpx.Response(200, json=raw),
        ]
    )

    client = WikiClient(settings=_settings(http_max_retries=2))
    try:
        detail = client.get_commodity_detail("laranite")
    finally:
        client.close()

    assert detail.slug == "laranite"
    assert route.call_count == 2


@respx.mock
def test_404_html_body_raises_client_error_without_retry(no_real_sleep):
    # Empirically, a non-existent slug returns 404 with an HTML body, not
    # JSON -- confirms the status check happens before `.json()` is ever
    # attempted, and that non-retryable errors don't trigger backoff.
    route = respx.get(f"{BASE_URL}/commodities/does-not-exist").mock(
        return_value=httpx.Response(
            404, headers={"Content-Type": "text/html"}, text="<html>404 - Signal Lost</html>"
        )
    )

    client = WikiClient(settings=_settings(http_max_retries=2))
    try:
        with pytest.raises(WikiApiClientError):
            client.get_commodity_detail("does-not-exist")
    finally:
        client.close()

    assert route.call_count == 1
    assert no_real_sleep == []


@respx.mock
def test_timeout_is_retried_then_gives_up(no_real_sleep):
    route = respx.get(f"{BASE_URL}/commodities/laranite").mock(
        side_effect=httpx.ReadTimeout("timed out")
    )

    client = WikiClient(settings=_settings(http_max_retries=1))
    try:
        with pytest.raises(WikiApiTransientError):
            client.get_commodity_detail("laranite")
    finally:
        client.close()

    assert route.call_count == 2  # 1 initial + 1 retry
    assert len(no_real_sleep) == 1


# --- redirect handling ----------------------------------------------------


@respx.mock
def test_follows_redirect_transparently(no_real_sleep):
    """Regression test for the real bug where the wiki API's own
    `links.next` pagination field returns an absolute `http://` URL (for
    real, observed on `/commodities` page 2) that the live API 301-redirects
    to the `https://` version. httpx defaults `follow_redirects` to `False`
    (unlike `requests`), so without `follow_redirects=True` on the
    underlying `httpx.Client`, the client would receive the redirect's HTML
    body and blow up trying to parse it as JSON instead of transparently
    following it. Doesn't need to be pagination specifically to prove the
    fix -- any request path redirecting is sufficient."""
    raw = _load_fixture("wiki_commodity_laranite.json")
    redirect_route = respx.get(f"{BASE_URL}/commodities/laranite").mock(
        return_value=httpx.Response(
            301,
            headers={"Location": f"{BASE_URL}/commodities/laranite-redirected"},
            text="<html>301 Moved Permanently</html>",
        )
    )
    target_route = respx.get(f"{BASE_URL}/commodities/laranite-redirected").mock(
        return_value=httpx.Response(200, json=raw)
    )

    client = WikiClient(settings=_settings())
    try:
        detail = client.get_commodity_detail("laranite")
    finally:
        client.close()

    assert redirect_route.call_count == 1
    assert target_route.call_count == 1
    assert detail.slug == "laranite"
    assert len(detail.purchase_entries) == 26
    # A followed redirect isn't a retryable failure -- no backoff wait.
    assert no_real_sleep == []


# --- live smoke test (excluded by default; run with `pytest -m live`) --


@pytest.mark.live
def test_live_get_commodity_detail_laranite_smoke():
    """Hits the real API once for a known-stable commodity slug.

    Not a correctness test -- just an early warning for schema drift on
    this specific client. Excluded from the default run.
    """
    client = WikiClient()
    try:
        detail = client.get_commodity_detail("laranite")
    finally:
        client.close()

    assert detail.slug == "laranite"
    assert detail.name
    assert len(detail.purchase_entries) >= 1
    entry = detail.purchase_entries[0]
    assert entry.terminal_id is not None
    assert entry.terminal_name
    assert entry.price_buy is not None or entry.price_sell is not None
