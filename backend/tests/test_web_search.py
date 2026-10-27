"""web_search sidecar：SSRF 防护、重定向、大小与类型、WAF、去重、落盘、超时。

出处：docs/04_tools/06_web_search_service.md §4.1、§7、§8。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

from baseagent_config.models import WebSearchConfig
from services.web_search.app import WebServices, create_app, fingerprint, normalise_url
from services.web_search.extract import extract_text
from services.web_search.netguard import (
    AddressRejected,
    SafeFetcher,
    check_url,
    ip_is_public,
)
from services.web_search.providers import DdgsProvider, DuckDuckGoProvider, SearchBlocked, SearchHit

PUBLIC = "93.184.216.34"
PUBLIC_B = "151.101.1.69"


def resolver(table: dict[str, list[str]]):  # type: ignore[no-untyped-def]
    async def resolve(host: str, port: int) -> list[str]:
        if host not in table:
            raise OSError("no such host")
        return table[host]

    return resolve


# ==============================================================================
# 地址校验
# ==============================================================================


@pytest.mark.security
@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "127.1.2.3",
        "10.0.0.5",
        "172.16.0.1",
        "172.31.255.255",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "224.0.0.1",
        "240.0.0.1",
        "100.64.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "fd12:3456::1",
        "::",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "not-an-ip",
    ],
)
def test_internal_and_reserved_addresses_are_not_public(address: str) -> None:
    assert not ip_is_public(address)


@pytest.mark.unit
@pytest.mark.parametrize("address", [PUBLIC, PUBLIC_B, "2606:4700:4700::1111"])
def test_public_addresses_are_public(address: str) -> None:
    assert ip_is_public(address)


@pytest.mark.security
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data",
        "http://2130706433/",
        "http://0x7f.0.0.1/",
        "http://0177.0.0.1/",
        "http://127.1/",
        "http://10.0.0.1:80/",
        "http://192.168.0.1/",
        "http://[::ffff:7f00:1]/",
        "ftp://example.com/",
        "file:///etc/passwd",
        "gopher://example.com/",
        "javascript:alert(1)",
        "http://user:pass@example.com/",
        "http://evil@example.com/",
        "http:///nohost",
        "http://example.com:8000/",
        "http://example.com:22/",
    ],
)
async def test_dangerous_urls_are_rejected_before_any_connection(url: str) -> None:
    table = {"localhost": ["127.0.0.1"], "example.com": [PUBLIC]}
    with pytest.raises(AddressRejected):
        await check_url(url, allowed_ports={80, 443}, resolver=resolver(table))


@pytest.mark.unit
async def test_public_urls_are_accepted_and_pinned_to_the_resolved_address() -> None:
    target = await check_url(
        "https://example.com/a?b=1",
        allowed_ports={80, 443},
        resolver=resolver({"example.com": [PUBLIC]}),
    )
    assert (target.host, target.port, target.ip) == ("example.com", 443, PUBLIC)


@pytest.mark.security
async def test_a_hostname_with_any_internal_resolution_is_rejected_as_a_whole() -> None:
    table = {"mixed.example": [PUBLIC, "10.0.0.7"], "nowhere.example": []}
    for host in table:
        with pytest.raises(AddressRejected):
            await check_url(f"http://{host}/", allowed_ports={80}, resolver=resolver(table))
    with pytest.raises(AddressRejected):
        await check_url("http://unresolvable.example/", allowed_ports={80}, resolver=resolver({}))


@pytest.mark.security
def test_fake_ip_range_is_rejected_by_default_and_only_that_range_is_opened() -> None:
    assert not ip_is_public("198.18.0.67")
    assert not ip_is_public("198.19.255.1")
    assert ip_is_public("198.18.0.67", allow_fake_ip=True)
    assert ip_is_public("198.19.255.1", allow_fake_ip=True)
    # 开关只放行 fake-ip 网段，回环、私网、元数据地址仍然拒绝
    for address in ("127.0.0.1", "10.0.0.5", "169.254.169.254", "::ffff:10.0.0.1"):
        assert not ip_is_public(address, allow_fake_ip=True)


@pytest.mark.security
async def test_fake_ip_resolution_needs_the_explicit_switch() -> None:
    table = {"site.example": ["198.18.0.67"]}
    with pytest.raises(AddressRejected):
        await check_url("https://site.example/", allowed_ports={443}, resolver=resolver(table))
    target = await check_url(
        "https://site.example/", allowed_ports={443}, resolver=resolver(table), allow_fake_ip=True
    )
    assert target.ip == "198.18.0.67"


@pytest.mark.unit
async def test_private_blocking_can_be_disabled_for_local_testing() -> None:
    target = await check_url(
        "http://127.0.0.1:80/", allowed_ports={80}, resolver=resolver({}), block_private=False
    )
    assert target.ip == "127.0.0.1"


# ==============================================================================
# 抓取
# ==============================================================================


def fetcher(handler, table=None, **kw: Any) -> SafeFetcher:  # type: ignore[no-untyped-def]
    return SafeFetcher(
        allowed_ports=kw.pop("allowed_ports", {80, 443}),
        max_redirects=kw.pop("max_redirects", 3),
        max_bytes=kw.pop("max_bytes", 10_000),
        allowed_types=kw.pop("allowed_types", ["text/html", "text/plain"]),
        user_agent="test-agent",
        timeout_sec=5,
        resolver=resolver(table or {"example.com": [PUBLIC], "other.example": [PUBLIC_B]}),
        transport=httpx.MockTransport(handler),
        **kw,
    )


def html(body: str, status: int = 200, ctype: str = "text/html; charset=utf-8") -> httpx.Response:
    return httpx.Response(status, content=body.encode(), headers={"content-type": ctype})


@pytest.mark.integration
async def test_fetch_connects_to_the_validated_ip_with_the_original_host_and_sni() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return html("<p>hello</p>")

    result = await fetcher(handler).fetch("https://example.com/page")
    assert result.status == "OK" and b"hello" in result.body
    request = seen[0]
    assert request.url.host == PUBLIC  # 连接用的是校验过的 IP，不再二次解析
    assert request.headers["host"] == "example.com"
    assert request.extensions["sni_hostname"] == "example.com"
    assert request.headers["user-agent"] == "test-agent"


@pytest.mark.security
async def test_dns_rebinding_is_defeated_because_the_resolution_is_used_once() -> None:
    answers = iter([[PUBLIC], ["127.0.0.1"]])

    async def flipping(host: str, port: int) -> list[str]:
        return next(answers)

    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return html("ok")

    f = SafeFetcher(
        allowed_ports={80},
        max_redirects=2,
        max_bytes=1000,
        allowed_types=["text/html"],
        user_agent="t",
        timeout_sec=5,
        resolver=flipping,
        transport=httpx.MockTransport(handler),
    )
    assert (await f.fetch("http://rebind.example/")).status == "OK"
    assert seen == [PUBLIC]  # 第二次解析（内网）根本没有发生，也没有被用来连接


@pytest.mark.security
async def test_redirects_are_followed_manually_and_every_hop_is_checked() -> None:
    def to_internal(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "example.com":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
        raise AssertionError("must not connect to the internal address")

    blocked = await fetcher(to_internal).fetch("http://example.com/")
    assert blocked.status == "ERROR" and "不被允许" in blocked.error

    def to_metadata(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    assert "不被允许" in (await fetcher(to_metadata).fetch("http://example.com/")).error

    def to_scheme(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "file:///etc/passwd"})

    assert (await fetcher(to_scheme).fetch("http://example.com/")).status == "ERROR"


@pytest.mark.integration
async def test_public_redirects_work_relative_ones_too_and_chains_are_capped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/a":
            return httpx.Response(302, headers={"location": "/b"})
        if path == "/b":
            return httpx.Response(301, headers={"location": "http://other.example/c"})
        return html("landed")

    ok = await fetcher(handler).fetch("http://example.com/a")
    assert ok.status == "OK" and ok.final_url == "http://other.example/c"

    def loop(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/again"})

    capped = await fetcher(loop, max_redirects=3).fetch("http://example.com/")
    assert capped.status == "ERROR" and "重定向超过 3 跳" in capped.error


@pytest.mark.security
async def test_response_size_is_capped_and_marked_truncated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return html("x" * 50_000)

    result = await fetcher(handler, max_bytes=1000).fetch("http://example.com/")
    assert result.status == "OK" and result.truncated and len(result.body) == 1000


@pytest.mark.unit
async def test_binary_and_unlisted_content_types_are_skipped() -> None:
    for ctype in ("application/pdf", "image/png", "application/zip"):
        r = await fetcher(lambda req, c=ctype: html("bin", ctype=c)).fetch("http://example.com/")
        assert r.status == "ERROR" and "内容类型" in r.error


@pytest.mark.unit
@pytest.mark.parametrize("status", [403, 429, 503])
async def test_waf_statuses_are_blocked_without_retry(status: int) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status)

    assert (await fetcher(handler).fetch("http://example.com/")).status == "BLOCKED"
    assert len(calls) == 1  # 不重试、不对抗


@pytest.mark.unit
async def test_other_errors_are_states_not_exceptions() -> None:
    assert (
        await fetcher(lambda r: httpx.Response(404)).fetch("http://example.com/")
    ).status == "ERROR"

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert (await fetcher(boom).fetch("http://example.com/")).status == "ERROR"


# ==============================================================================
# 清洗与提供者
# ==============================================================================


@pytest.mark.unit
def test_extraction_drops_scripts_and_keeps_headings_lists_and_code() -> None:
    page = (
        "<html><head><title>T</title><script>evil()</script></head><body><nav>menu</nav>"
        "<h1>Title</h1><p>Some   text.</p><ul><li>one</li><li>two</li></ul>"
        "<pre>def f():\n    return 1</pre><style>.x{}</style><footer>foot</footer></body></html>"
    )
    text = extract_text(page)
    assert "evil" not in text and "menu" not in text and "foot" not in text and ".x{}" not in text
    assert "# Title" in text and "- one" in text and "Some text." in text
    assert "```\ndef f():\n    return 1\n```" in text


@pytest.mark.unit
def test_url_normalisation_and_fingerprint() -> None:
    assert normalise_url("HTTP://Example.COM:80/a/#frag") == normalise_url("http://example.com/a")
    assert normalise_url("https://example.com:443") == "https://example.com/"
    assert normalise_url("http://example.com/a?x=1") != normalise_url("http://example.com/a?x=2")
    assert fingerprint("  same ") == fingerprint("same")


@pytest.mark.unit
async def test_duckduckgo_results_are_parsed_and_redirect_links_unwrapped() -> None:
    page = (
        '<div class="result"><a class="result__a" '
        'href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&rut=x">'
        "Python  Docs</a>"
        '<a class="result__snippet">The official   docs</a></div>'
        '<div class="result"><a class="result__a" href="javascript:alert(1)">bad</a></div>'
        '<div class="result"><a class="result__a" href="https://example.com/x">Ex</a></div>'
    )
    provider = DuckDuckGoProvider(
        user_agent="t",
        timeout_sec=5,
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page)),
    )
    hits = await provider.search("python", 10)
    assert [h.url for h in hits] == ["https://docs.python.org/3/", "https://example.com/x"]
    assert hits[0].title == "Python Docs" and hits[0].snippet == "The official docs"
    assert (await provider.search("python", 1))[0].url == "https://docs.python.org/3/"


@pytest.mark.unit
async def test_duckduckgo_challenge_page_is_a_block_not_an_empty_result() -> None:
    provider = DuckDuckGoProvider(
        user_agent="t",
        timeout_sec=5,
        transport=httpx.MockTransport(lambda r: httpx.Response(202, text="<html>anomaly</html>")),
    )
    with pytest.raises(SearchBlocked):
        await provider.search("python", 5)


def fake_ddgs(monkeypatch: pytest.MonkeyPatch, script: dict[str, Any]) -> list[str]:
    """按后端名给出结果（列表）或异常；返回被尝试过的后端顺序。"""
    tried: list[str] = []

    class FakeDDGS:
        def __init__(self, **_: Any) -> None: ...

        def text(self, query: str, *, backend: str, **_: Any) -> list[dict[str, str]]:
            tried.append(backend)
            outcome = script[backend]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

    monkeypatch.setattr("ddgs.DDGS", FakeDDGS)
    return tried


ROWS = [
    {"title": " A  b ", "href": "https://a.example/x", "body": "s  1"},
    {"title": "bad", "href": "javascript:1", "body": ""},
]


@pytest.mark.unit
async def test_ddgs_provider_maps_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    tried = fake_ddgs(monkeypatch, {"yahoo": ROWS})
    hits = await DdgsProvider(timeout_sec=5, backends=["yahoo"]).search("q", 5)
    assert hits == [SearchHit("A b", "https://a.example/x", "s 1")] and tried == ["yahoo"]


@pytest.mark.unit
async def test_ddgs_provider_falls_through_empty_and_failing_backends_to_the_first_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ddgs.exceptions import DDGSException

    tried = fake_ddgs(
        monkeypatch,
        {
            "yahoo": DDGSException("No results found."),
            "bing": DDGSException("Timeout"),
            "google": ROWS,
        },
    )
    provider = DdgsProvider(timeout_sec=5, backends=["yahoo", "bing", "google"])
    assert [h.url for h in await provider.search("q", 5)] == ["https://a.example/x"]
    assert tried == ["yahoo", "bing", "google"]


@pytest.mark.unit
async def test_ddgs_provider_stops_at_the_first_backend_with_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tried = fake_ddgs(monkeypatch, {"yahoo": ROWS, "bing": ROWS})
    await DdgsProvider(timeout_sec=5, backends=["yahoo", "bing"]).search("q", 5)
    assert tried == ["yahoo"]


@pytest.mark.unit
async def test_ddgs_provider_distinguishes_empty_from_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ddgs.exceptions import DDGSException

    empty = DDGSException("No results found.")
    fake_ddgs(monkeypatch, {"yahoo": empty, "bing": empty})
    assert await DdgsProvider(timeout_sec=5, backends=["yahoo", "bing"]).search("q", 5) == []
    # 全部后端都是错误（不是“没有结果”）：必须作为被拦截上报
    boom = DDGSException("Timeout")
    fake_ddgs(monkeypatch, {"yahoo": boom, "bing": boom})
    with pytest.raises(SearchBlocked):
        await DdgsProvider(timeout_sec=5, backends=["yahoo", "bing"]).search("q", 5)


@pytest.mark.unit
async def test_ddgs_provider_gives_up_the_chain_when_the_deadline_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ddgs.exceptions import DDGSException

    tried = fake_ddgs(monkeypatch, {"a": DDGSException("Timeout"), "b": ROWS})
    provider = DdgsProvider(timeout_sec=5, backends=["a", "b"], deadline_sec=0)
    assert await provider.search("q", 5) == []  # 时限已到，不再试 b
    assert tried == ["a"]


# ==============================================================================
# 服务
# ==============================================================================


class FakeProvider:
    name = "fake"
    needs_key = False
    may_return_private_links = True

    def __init__(
        self, hits: list[SearchHit], delay: float = 0.0, error: Exception | None = None
    ) -> None:
        self.hits, self.delay, self.error = hits, delay, error

    async def search(self, query: str, limit: int) -> list[SearchHit]:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error:
            raise self.error
        return self.hits[:limit]


def make_client(tmp_path: Path, hits: list[SearchHit], handler, **cfg: Any):  # type: ignore[no-untyped-def]
    config = WebSearchConfig(
        **{"max_content_tokens": 50, "preview_chars": 40, "total_timeout_sec": 5, **cfg}
    )
    provider = cfg.pop("provider_obj", None) or FakeProvider(hits)
    services = WebServices(
        config=config, provider=provider, fetcher=fetcher(handler), artifacts_dir=tmp_path / "art"
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)), base_url="http://127.0.0.1:8003"
    )


def body(**kw: Any) -> dict[str, Any]:
    return {"query": "python", "task_id": "task_1", **kw}


@pytest.mark.integration
async def test_search_fetches_cleans_and_reports_each_result(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["host"] == "example.com":
            return html("<h1>Short</h1><p>tiny page</p>")
        return httpx.Response(403)

    hits = [
        SearchHit("A", "https://example.com/a", "snip a"),
        SearchHit("B", "https://other.example/b", "snip b"),
    ]
    async with make_client(tmp_path, hits, handler) as client:
        response = await client.post("/api/v1/search/query", json=body())
    data = response.json()
    assert response.status_code == 200 and data["error"] is None
    ok, blocked = data["results"]
    assert ok["status"] == "OK" and "# Short" in ok["preview"] and ok["artifact_path"] is None
    assert blocked["status"] == "BLOCKED" and blocked["snippet"] == "snip b"


@pytest.mark.integration
async def test_long_pages_are_written_to_the_task_folder_and_only_a_preview_is_returned(
    tmp_path: Path,
) -> None:
    long_page = "<p>" + "word " * 500 + "</p>"
    hits = [SearchHit("L", "https://example.com/long", "s")]
    async with make_client(tmp_path, hits, lambda r: html(long_page)) as client:
        data = (await client.post("/api/v1/search/query", json=body())).json()
    item = data["results"][0]
    assert item["status"] == "OK" and item["is_truncated"] is True and len(item["preview"]) <= 40
    path = Path(item["artifact_path"])
    assert path.parent == tmp_path / "art" / "task_1" and path.name.startswith("web_")
    assert path.read_text().count("word") == 500  # noqa: ASYNC240


@pytest.mark.security
async def test_internal_links_from_the_search_provider_are_never_fetched(tmp_path: Path) -> None:
    hits = [
        SearchHit("meta", "http://169.254.169.254/latest/meta-data/", "cloud metadata"),
        SearchHit("local", "http://127.0.0.1:8000/api/v1/workspaces", "agent api"),
        SearchHit("sidecar", "http://localhost:8002/health", "sidecar"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"connected to {request.url}")

    async with make_client(tmp_path, hits, handler) as client:
        data = (await client.post("/api/v1/search/query", json=body())).json()
    assert [r["status"] for r in data["results"]] == ["ERROR"] * 3
    assert all("不被允许" in r["error"] for r in data["results"])


@pytest.mark.integration
async def test_fetch_endpoint_fetches_only_the_given_pages_with_the_same_address_checks(
    tmp_path: Path,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return html("<h1>Page</h1><p>body text</p>")

    items = [
        {"url": "https://example.com/a", "title": "A", "snippet": "sa"},
        {"url": "http://169.254.169.254/latest/meta-data/"},
    ]
    async with make_client(tmp_path, [], handler) as client:
        response = await client.post("/api/v1/search/fetch", json={"items": items, "task_id": "t1"})
        bad = await client.post("/api/v1/search/fetch", json={"items": [], "task_id": "t1"})
        evil = await client.post(
            "/api/v1/search/fetch", json={"items": items[:1], "task_id": "../x"}
        )
    ok, blocked = response.json()["results"]
    assert ok["status"] == "OK" and ok["title"] == "A" and "# Page" in ok["preview"]
    assert blocked["status"] == "ERROR" and "不被允许" in blocked["error"]
    assert len(seen) == 1 and bad.status_code == 400 and evil.status_code == 422


@pytest.mark.unit
async def test_duplicates_are_collapsed_by_url_and_by_content(tmp_path: Path) -> None:
    hits = [
        SearchHit("1", "https://example.com/a", ""),
        SearchHit("1b", "HTTPS://EXAMPLE.COM:443/a/#top", ""),
        SearchHit("2", "https://other.example/mirror", ""),
    ]
    async with make_client(tmp_path, hits, lambda r: html("<p>same content</p>")) as client:
        data = (await client.post("/api/v1/search/query", json=body())).json()
    assert [r["url"] for r in data["results"]] == ["https://example.com/a"]


@pytest.mark.unit
async def test_fetch_false_returns_snippets_and_max_results_is_capped(tmp_path: Path) -> None:
    hits = [SearchHit(str(i), f"https://example.com/{i}", f"s{i}") for i in range(20)]

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not fetch")

    async with make_client(tmp_path, hits, handler, max_results=4) as client:
        data = (
            await client.post("/api/v1/search/query", json=body(fetch=False, max_results=99))
        ).json()
    assert len(data["results"]) == 4 and {r["status"] for r in data["results"]} == {"SNIPPET_ONLY"}


@pytest.mark.security
@pytest.mark.parametrize("task_id", ["../x", "a/b", "", "x" * 65, ".hidden"])
async def test_task_ids_cannot_escape_the_artifact_root(tmp_path: Path, task_id: str) -> None:
    async with make_client(tmp_path, [], lambda r: html("x")) as client:
        response = await client.post("/api/v1/search/query", json=body(task_id=task_id))
    assert response.status_code in (400, 422)


@pytest.mark.unit
async def test_total_timeout_is_504_and_provider_failure_is_a_state(tmp_path: Path) -> None:
    slow = FakeProvider([SearchHit("A", "https://example.com/a", "")], delay=3)
    config = WebSearchConfig(total_timeout_sec=1)
    services = WebServices(
        config=config, provider=slow, fetcher=fetcher(lambda r: html("x")), artifacts_dir=tmp_path
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)), base_url="http://127.0.0.1:8003"
    ) as client:
        assert (await client.post("/api/v1/search/query", json=body())).status_code == 504
    broken = FakeProvider([], error=RuntimeError("provider down"))
    services = WebServices(
        config=WebSearchConfig(),
        provider=broken,
        fetcher=fetcher(lambda r: html("x")),
        artifacts_dir=tmp_path,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(services)), base_url="http://127.0.0.1:8003"
    ) as client:
        data = (await client.post("/api/v1/search/query", json=body())).json()
    assert data["results"] == [] and "RuntimeError" in data["error"]


@pytest.mark.security
async def test_cross_site_requests_and_bad_bodies_are_refused(tmp_path: Path) -> None:
    async with make_client(tmp_path, [], lambda r: html("x")) as client:
        cross = await client.post(
            "/api/v1/search/query", json=body(), headers={"Origin": "https://evil.example"}
        )
        assert cross.status_code == 403
        rebound = await client.post(
            "/api/v1/search/query", json=body(), headers={"Host": "evil.example"}
        )
        assert rebound.status_code == 403
        assert (await client.post("/api/v1/search/query", json={"query": ""})).status_code == 400
        assert (await client.get("/api/v1/health")).json()["status"] == "ok"
