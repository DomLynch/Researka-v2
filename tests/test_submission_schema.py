"""Bounded generated requests against an in-memory ASGI app, never a live URL."""
import socket

import pytest
import schemathesis
from hypothesis import HealthCheck, Phase, settings
from schemathesis.checks import not_a_server_error


@pytest.fixture
def submission_schema(client, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Schema tests must not contact external services")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    return schemathesis.openapi.from_asgi("/openapi.json", client.app)


schema = schemathesis.pytest.from_fixture("submission_schema").include(path="/submissions", method="POST")


@schema.parametrize()
@settings(max_examples=20, deadline=None, derandomize=True, phases=[Phase.generate, Phase.shrink], suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_generated_submission_never_publishes(case, inmemory_repo):
    assert case.path == "/submissions" and case.method == "POST"
    inmemory_repo.reset()
    response = case.call(headers={"x-api-key": "test-legacy-key"})
    # OpenAPI cannot express whether a parent exists. Check the specific domain
    # error instead of demanding every schema-valid request be accepted.
    case.validate_response(response, checks=[not_a_server_error])
    assert response.status_code in {200, 400, 422}
    if response.status_code == 400:
        assert response.json().get("detail") in {
            "parent_submission_not_found", "There was an error parsing the body",
        }
    assert not inmemory_repo.list_objects("publication")
    if response.status_code == 200:
        submission = response.json()["submission"]
        assert inmemory_repo.get_object(submission["id"]) is not None
