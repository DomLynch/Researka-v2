"""Exercise the real intake boundary with synthetic data and no network."""
import hashlib
import json
import socket
import sys

import pytest

from scripts.replay_intake_contract import main, replay
from tests.test_zero_trust_gates import _bundle, _sections


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Publication-contract tests must not contact external services")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


@pytest.mark.parametrize("bundled", [True, False])
def test_citation_replay_reaches_real_intake(client, inmemory_repo, tmp_path, snapshot, bundled):
    doi = "10.1234/study(alpha)"
    bundle = _bundle()
    if bundled:
        bundle[0]["doi"] = doi
    payload = {
        "title": "Synthetic intake contract replay",
        "abstract": "Synthetic bounded intake regression; not a publishable paper.",
        "sections": _sections(extra=f" See [source](https://doi.org/{doi})."),
        "source_bundle": bundle,
        "author_agent_id": "agent-contract-test",
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload))
    replayed = replay(path)
    assert replayed["gates"]["citation_membership"] is bundled
    assert replayed["input_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()

    response = client.post("/submissions", json=payload)
    assert response.status_code == 200, response.text
    submission_id = response.json()["submission"]["id"]
    pending = client.get(f"/submissions/{submission_id}/decision").json()
    assert pending["status"] == "pending" and pending["decision"] is None
    assert not inmemory_repo.list_objects("publication")
    result = client.post("/jobs/run-once", headers={"x-api-key": "test-admin-key"})
    assert result.status_code == 200
    decision = client.get(f"/submissions/{submission_id}/decision").json()
    # Assert meaning before recording the snapshot; a 200 submission is not publication.
    assert not inmemory_repo.list_objects("publication")
    assert decision["decision"] == (None if bundled else "revise"), decision
    assert {
        "citation_membership": replayed["gates"]["citation_membership"],
        "failed_template_gates": replayed["failed_gates"],
        "decision_after_intake": decision["decision"],
        "publication_count": len(inmemory_repo.list_objects("publication")),
    } == snapshot


def test_export_replay_requires_exact_identity(tmp_path):
    path = tmp_path / "export.json"
    request = {"sections": _sections(), "source_bundle": _bundle()}
    record = {"type": "submission", "id": "parent", "title": "Synthetic", "metadata": request}
    path.write_text(json.dumps([record, {**record, "id": "child"}]))
    with pytest.raises(ValueError, match="requires --submission-id"):
        replay(path)
    with pytest.raises(ValueError, match="exactly one"):
        replay(path, "missing")
    assert replay(path, "parent")["submission_id"] == "parent"
    path.write_text(json.dumps([record, record]))
    with pytest.raises(ValueError, match="exactly one"):
        replay(path, "parent")


@pytest.mark.parametrize("payload", [{}, {"sections": {"Abstract": 1}}, {"sections": {"Abstract": "text"}, "source_bundle": [1]}])
def test_replay_rejects_incomplete_inputs(tmp_path, payload):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        replay(path)


@pytest.mark.parametrize("missing_source", [False, True])
def test_replay_cli_exit_codes(tmp_path, monkeypatch, capsys, missing_source):
    path = tmp_path / "request.json"
    path.write_text(json.dumps({
        "sections": _sections(extra=" See 10.9999/absent." if missing_source else ""),
        "source_bundle": _bundle(),
    }))
    monkeypatch.setattr(sys, "argv", ["replay", str(path)])
    assert main() == int(missing_source)
    assert json.loads(capsys.readouterr().out)["scope"] == "offline_template_gates_only"
    path.write_text("[null]")
    monkeypatch.setattr(sys, "argv", ["replay", str(path), "--submission-id", "parent"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert "Export records must be objects" in capsys.readouterr().err


@pytest.mark.parametrize("invalid", [{"title": None}, {"title": []}, {"article_type": None}, {"article_type": "unsupported"}])
def test_replay_cli_rejects_invalid_types(tmp_path, monkeypatch, capsys, invalid):
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"sections": _sections(), "source_bundle": _bundle(), **invalid}))
    monkeypatch.setattr(sys, "argv", ["replay", str(path)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert not captured.out
    assert captured.err.startswith("Invalid replay input:")
