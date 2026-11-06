"""End-to-end tests against a fake Telem router (response shapes from docs.telem.ai)."""
import json
import os

import httpx
import pytest

from congress_watch.cli import main
from congress_watch.store import Store
from congress_watch.telem_client import TelemClient


class FakeTelem:
    def __init__(self):
        self.monitors = {}
        self.calls = []
        self.queue = {}  # monitor_id -> list of poll responses
        self.n = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer tlm_test"
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else {}
        self.calls.append((method, path, body))

        if method == "POST" and path == "/v1/monitors":
            self.n += 1
            mid = f"mon_{self.n}"
            m = {"id": mid, "query": body["query"], "frequency": body["options"]["frequency"],
                 "status": "active", "created_at": "2026-11-05T18:00:00Z",
                 "expires_at": "2026-12-05T18:00:00Z", "webhook_secret": None}
            self.monitors[mid] = m
            return httpx.Response(201, json=m)
        if method == "POST" and path.endswith("/poll"):
            mid = path.split("/")[3]
            if mid not in self.monitors:
                return httpx.Response(404, json={"error": "not found"})
            q = self.queue.get(mid) or [{"results": []}]
            return httpx.Response(200, json=q.pop(0) if len(q) > 1 else q[0])
        if method == "PATCH":
            mid = path.split("/")[3]
            self.monitors[mid].update(body)
            return httpx.Response(200, json=self.monitors[mid])
        if method == "DELETE":
            self.monitors.pop(path.split("/")[3], None)
            return httpx.Response(204)
        if method == "POST" and path == "/v1/search":
            return httpx.Response(200, json={"status": "succeeded", "preprocessor_runs": [
                {"preprocessor_name": "exa", "output_payload": {"results": [
                    {"rank": 1, "url": "https://a.example/1", "title": "A1", "summary": "s"}]}},
                {"preprocessor_name": "brave", "output_payload": {"results": [
                    {"rank": 1, "url": "https://a.example/1", "title": "A1 dup"},
                    {"rank": 2, "url": "https://b.example/2", "title": "B2"}]}}]})
        if method == "POST" and path == "/v1/fetch":
            return httpx.Response(200, json={"status": "succeeded", "preprocessor_runs": [
                {"preprocessor_name": "web_fetch", "output_payload": {"fetched_results": [
                    {"url": u, "status": "succeeded", "content": f"# Page {u}"} for u in body["urls"]]}}]})
        return httpx.Response(404)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "topics.yaml").write_text(
        "defaults: {frequency: 1d, ttl: 30d}\n"
        "topics:\n"
        "  - {key: senate-alaska, label: 'Alaska Senate', query: 'Alaska race call', frequency: 1h}\n"
        "  - {key: leadership, label: 'Leadership', query: 'leadership votes',"
        " include_domains: [house.gov]}\n"
    )
    fake = FakeTelem()
    client = TelemClient(api_key="tlm_test", transport=httpx.MockTransport(fake.handler))
    return fake, client, tmp_path


def test_setup_is_idempotent_and_sends_documented_body(env):
    fake, client, _ = env
    assert main(["setup"], client=client) == 0
    creates = [c for c in fake.calls if c[1] == "/v1/monitors"]
    assert len(creates) == 2
    body = creates[1][2]
    assert body["options"] == {"frequency": "1d", "include_domains": ["house.gov"]}
    assert body["ttl"] == "30d"
    fake.calls.clear()
    assert main(["setup"], client=client) == 0
    assert not [c for c in fake.calls if c[1] == "/v1/monitors"]  # nothing recreated


def test_poll_dedupes_and_brief(env, capsys):
    fake, client, tmp = env
    main(["setup"], client=client)
    fake.queue["mon_1"] = [
        {"results": [{"title": "AP: Alaska count update", "url": "https://x.example/ak"}],
         "warnings": ["more_runs_pending"]},
        {"results": [{"title": "AP: Alaska count update", "url": "https://x.example/ak"},
                     {"title": "Alaska officials release ballots", "url": "https://y.example/ak2",
                      "summary": "Election officials released..."}]},
    ]
    assert main(["poll"], client=client) == 0
    s = Store("congress_watch.db")
    assert len(s.unbriefed()) == 2  # duplicate URL stored once

    assert main(["brief"], client=client) == 0
    md_files = list((tmp / "briefs").glob("*.md"))
    assert len(md_files) == 1 and md_files[0].with_suffix(".html").exists()
    text = md_files[0].read_text()
    assert "## Alaska Senate" in text and "## Sources" in text and "https://y.example/ak2" in text
    assert s.unbriefed() == []

    main(["brief"], client=client)
    assert "No new results" in capsys.readouterr().out


def test_expired_monitor_is_flagged_then_recreated(env):
    fake, client, _ = env
    main(["setup"], client=client)
    fake.queue["mon_1"] = [{"results": [], "monitor_inactive": True}]
    main(["poll"], client=client)
    s = Store("congress_watch.db")
    assert {m["topic_key"]: m["status"] for m in s.monitors()}["senate-alaska"] == "ended"
    main(["setup"], client=client)
    assert {m["topic_key"]: m["monitor_id"] for m in s.monitors()}["senate-alaska"] == "mon_3"


def test_poll_reads_search_shaped_response_and_first_run_pending(env, capsys, tmp_path):
    fake, client, _ = env
    main(["setup"], client=client)
    fake.queue["mon_1"] = [{"status": "succeeded", "preprocessor_runs": [
        {"preprocessor_name": "exa", "output_payload": {"results": [
            {"url": "https://n.example/1", "title": "Alaska tally update"}]}},
        {"preprocessor_name": "brave", "output_payload": {"results": [
            {"url": "https://n.example/1", "title": "dup"},
            {"url": "https://n.example/2", "title": "Second"}]}}]}]
    fake.queue["mon_2"] = [{"results": [], "warnings": [{"code": "first_run_pending"}]}]
    main(["poll", "--raw", "raw"], client=client)
    out = capsys.readouterr().out
    assert "senate-alaska          +2" in out
    assert "first run still in progress" in out and "Poll again" in out
    assert (tmp_path / "raw" / "senate-alaska_0.json").exists()


def test_poll_explains_empty_db_and_skipped(env, capsys):
    fake, client, _ = env
    assert main(["poll"], client=client) == 1
    assert "No monitors in this database" in capsys.readouterr().out
    main(["setup"], client=client)
    Store("congress_watch.db").set_monitor_status("leadership", "ended")
    fake.monitors.pop("mon_1")  # make senate-alaska return 404
    main(["poll", "--raw", "raw"], client=client)
    out = capsys.readouterr().out
    assert "leadership             skipped (status: ended)" in out
    assert "monitor not found" in out


def test_search_merges_providers(env):
    _, client, _ = env
    res = client.search("x")
    assert [r["url"] for r in res] == ["https://a.example/1", "https://b.example/2"]


def test_brief_uses_whole_sentences_only(env, monkeypatch):
    fake, client, tmp = env
    main(["setup"], client=client)
    fake.queue["mon_1"] = [{"results": [
        {"title": "Alaska count", "url": "https://z.example/a",
         "summary": "Nov 5, 2026 ... ballots were being counted in Anchorage ... The AP has not "
                    "called the race. Officials expect another update Friday. More to come as the"},
        {"title": "Fragment only", "url": "https://z.example/b",
         "summary": "Nov 5, 2026 ... the senator told reporters that..."},
    ]}]
    main(["poll"], client=client)

    orig = fake.handler

    def handler(request):
        if request.url.path == "/v1/fetch":
            urls = json.loads(request.content)["urls"]
            assert urls == ["https://z.example/b"]  # only the fragment-only item is fetched
            return httpx.Response(200, json={"preprocessor_runs": [{"output_payload": {
                "fetched_results": [{"url": urls[0], "status": "succeeded", "content":
                    "# Headline\n\nBy A Reporter\n\nWASHINGTON (AP) — The senator told reporters on "
                    "Thursday that she would not seek a leadership post in the next Congress. Her "
                    "office confirmed the decision in a written statement."}]}}]})
        return orig(request)

    client._http._transport = httpx.MockTransport(handler)
    main(["brief"], client=client)
    text = next((tmp / "briefs").glob("*.md")).read_text()
    assert "The AP has not called the race. Officials expect another update Friday." in text
    assert "More to come" not in text and "..." not in text.split("## Sources")[0]
    assert "WASHINGTON (AP) — The senator told reporters on Thursday" in text
