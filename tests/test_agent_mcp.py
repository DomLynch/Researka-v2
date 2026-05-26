from apps.agent_mcp import server


def test_submit_research_paper_wraps_current_submission_contract(monkeypatch):
    captured = {}

    def fake_submit(api_key, payload):
        captured["api_key"] = api_key
        captured["payload"] = payload
        return {"ok": True}

    monkeypatch.setattr(server, "_submit", fake_submit)

    result = server.submit_research_paper(
        api_key="rk_test",
        author_agent_id="agent-a",
        title="Paper",
        abstract="Abstract",
        body_markdown="Body",
        source_bundle=[{"title": "Source", "year": 2025, "evidence_type": "review"}],
        domain_slug="longevity",
    )

    assert result == {"ok": True}
    assert captured["api_key"] == "rk_test"
    assert captured["payload"]["article_type"] == "research_synthesis"
    assert captured["payload"]["author_agent_id"] == "agent-a"
    assert captured["payload"]["source_bundle"][0]["title"] == "Source"


def test_submit_alpha_memo_forces_alpha_contract(monkeypatch):
    captured = {}
    monkeypatch.setattr(server, "_submit", lambda api_key, payload: captured.setdefault("payload", payload))

    result = server.submit_alpha_memo(
        api_key="rk_test",
        author_agent_id="agent-v4",
        title="Alpha",
        markdown="# Alpha",
        evidence_bundle={"publish_verdict": {"decision": "ready_to_publish"}},
    )

    assert result["artifact_type"] == "alpha_memo"
    assert result["article_type"] == "alpha_memo"
    assert result["abstract"] == "Alpha"
    assert result["body_markdown"] == "# Alpha"


def test_read_tools_quote_ids(monkeypatch):
    calls = []
    monkeypatch.setattr(server, "_request", lambda method, path, **kwargs: calls.append((method, path)) or {"ok": True})

    server.get_decision("id with space")
    server.get_provenance("id/with/slash")
    server.get_timeline("id")
    server.get_publication("pub id")

    assert calls == [
        ("GET", "/submissions/id%20with%20space/decision"),
        ("GET", "/submissions/id%2Fwith%2Fslash/provenance"),
        ("GET", "/submissions/id/timeline"),
        ("GET", "/publications/pub%20id"),
    ]
