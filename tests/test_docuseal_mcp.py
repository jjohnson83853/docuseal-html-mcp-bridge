import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import docuseal_mcp


class FakeSession:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def initialize(self):
        return None


    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.error:
            raise self.error
        return self.result


class MockMcpTransport:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.session = FakeSession(result, error)
        self.url = None
        self.http_client = None
        self.closed = False

    @asynccontextmanager
    async def connect(self, url, *, http_client):
        self.url = url
        self.http_client = http_client
        try:
            yield object(), object(), "session"
        finally:
            self.closed = True


class MockHttpClient:
    def __init__(self, *, headers, timeout):
        self.headers = headers
        self.timeout = timeout
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True
        return False


def result_with_text(text, *, is_error=False):
    return SimpleNamespace(
        isError=is_error, content=[SimpleNamespace(type="text", text=text)]
    )


def configure(monkeypatch, result=None, error=None):
    transport = MockMcpTransport(result, error)
    clients = []

    def make_http_client(*, headers, timeout):
        client = MockHttpClient(headers=headers, timeout=timeout)
        clients.append(client)
        return client

    monkeypatch.setattr(docuseal_mcp, "streamable_http_client", transport.connect)
    monkeypatch.setattr(docuseal_mcp, "ClientSession", lambda *args: transport.session)
    monkeypatch.setattr(docuseal_mcp.httpx, "AsyncClient", make_http_client)
    return transport, clients


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.mark.parametrize(
    "origin",
    [
        "https://mcp.example",
        "https://mcp.example/",
        "http://localhost",
        "http://localhost:8080/",
        "http://127.0.0.1",
        "http://[::1]/",
    ],
)
def test_client_derives_mcp_endpoint_from_origin(origin):
    client = docuseal_mcp.DocuSealMcpClient(origin, "token")

    assert client._url == origin.rstrip("/") + "/mcp"


@pytest.mark.parametrize(
    "origin",
    [
        "http://mcp.example",
        "/",
        "https:/// ",
        "https://user:password@mcp.example",
        "https://mcp.example/path",
        "https://mcp.example/mcp",
        "https://mcp.example?",
        "https://mcp.example#",
        "https://mcp.example:invalid",
        "https://mcp.example:99999",
        "https://[invalid]",
        "https://",
        "https://mcp.example:0",
        "https://mcp.example:65536",
        "https://mcp.example:443/path",
        "https://mcp.example/?",
        "https://mcp.example/#",
        "http://[2001:db8::1]",
    ],
)
def test_client_rejects_unsafe_origins(origin):
    with pytest.raises(ValueError, match="DOCUSEAL_URL"):
        docuseal_mcp.DocuSealMcpClient(origin, "token")


def test_search_maps_query_and_limit_and_returns_only_documented_fields(monkeypatch):
    transport, clients = configure(
        monkeypatch,
        result_with_text(json.dumps([{"id": 7, "name": "NDA", "slug": "discard"}])),
    )
    client = docuseal_mcp.DocuSealMcpClient(
        "https://mcp.example", "unit-test-secret"
    )

    response = run(client.search_templates("non-disclosure", 23))

    assert transport.session.calls == [
        ("search_templates", {"q": "non-disclosure", "limit": 23})
    ]
    assert transport.url == "https://mcp.example/mcp"
    assert clients[0].headers == {"Authorization": "Bearer unit-test-secret"}
    assert response == {"data": [{"id": 7, "name": "NDA"}]}
    assert "unit-test-secret" not in repr(response)
    assert clients[0].closed and transport.closed

@pytest.mark.parametrize(
    "result",
    [
        None,
        SimpleNamespace(isError=False, content=[]),
    ],
)
def test_rejects_malformed_or_failed_native_results(monkeypatch, result):
    configure(monkeypatch, result)
    client = docuseal_mcp.DocuSealMcpClient("https://mcp.example", "token")

    with pytest.raises(docuseal_mcp.DocuSealMcpError):
        run(client.search_templates())


@pytest.mark.parametrize("limit", [1, 100])
def test_search_accepts_limit_boundaries(monkeypatch, limit):
    transport, _ = configure(monkeypatch, result_with_text("[]"))
    client = docuseal_mcp.DocuSealMcpClient("https://mcp.example", "token")

    assert run(client.search_templates(limit=limit)) == {"data": []}
    assert transport.session.calls == [
        ("search_templates", {"q": "", "limit": limit})
    ]


@pytest.mark.parametrize("limit", [0, 101, True, "10"])
def test_search_rejects_invalid_limit_before_connecting(monkeypatch, limit):
    transport, _ = configure(monkeypatch, result_with_text("[]"))
    client = docuseal_mcp.DocuSealMcpClient("https://mcp.example", "token")

    with pytest.raises(ValueError, match="between 1 and 100"):
        run(client.search_templates(limit=limit))
    assert transport.url is None


def test_missing_token_has_named_safe_error_without_connecting(monkeypatch):
    transport, _ = configure(monkeypatch, result_with_text("[]"))
    client = docuseal_mcp.DocuSealMcpClient("https://mcp.example", "")

    with pytest.raises(
        docuseal_mcp.DocuSealMcpError, match="DOCUSEAL_MCP_BEARER_TOKEN"
    ):
        run(client.search_templates())
    assert transport.url is None

@pytest.mark.parametrize(
    "result",
    [
        None,
        SimpleNamespace(isError=False, content=[]),
        SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text="{}")]),
    ],
)
def test_search_rejects_malformed_native_results(monkeypatch, result):
    configure(monkeypatch, result)
    client = docuseal_mcp.DocuSealMcpClient("https://mcp.example", "token")

    with pytest.raises(docuseal_mcp.DocuSealMcpError):
        run(client.search_templates())

@pytest.mark.parametrize(
    "result",
    [
        result_with_text("[]", is_error=True),
        SimpleNamespace(isError=None, content=[SimpleNamespace(type="text", text="[]")]),
        SimpleNamespace(isError=False, content=[]),
        SimpleNamespace(isError=False, content=[SimpleNamespace(type="image", text=None)]),
        SimpleNamespace(isError=False, content=[SimpleNamespace(type="text", text="[]"), SimpleNamespace(type="text", text="[]")]),
        result_with_text("not json"),
        result_with_text('[{"id": true, "name": "bad"}]'),
        result_with_text('[{"id": 1, "name": 2}]'),
        result_with_text('[{"id": 1}]'),
        result_with_text("{}"),
    ],
)
def test_rejects_failed_or_malformed_results_without_upstream_text(monkeypatch, result):
    secret_text = "private-upstream-diagnostic"
    transport, _ = configure(monkeypatch, result)
    client = docuseal_mcp.DocuSealMcpClient("https://mcp.example", "unit-test-secret")

    with pytest.raises(docuseal_mcp.DocuSealMcpError) as error:
        run(client.search_templates())

    assert secret_text not in str(error.value)
    assert "unit-test-secret" not in str(error.value)

def test_error_result_does_not_expose_upstream_text(monkeypatch):
    private_message = "private-upstream-diagnostic"
    configure(monkeypatch, result_with_text(private_message, is_error=True))
    client = docuseal_mcp.DocuSealMcpClient(
        "https://mcp.example", "unit-test-secret"
    )

    with pytest.raises(docuseal_mcp.DocuSealMcpError) as error:
        run(client.search_templates())

    assert private_message not in str(error.value)
    assert "unit-test-secret" not in str(error.value)


def test_transport_error_is_safe_and_does_not_log_secret_or_detail(monkeypatch, caplog):
    transport, _ = configure(
        monkeypatch,
        error=RuntimeError("unit-test-secret private-upstream-diagnostic"),
    )
    client = docuseal_mcp.DocuSealMcpClient("https://mcp.example", "unit-test-secret")

    with pytest.raises(docuseal_mcp.DocuSealMcpError, match="Could not reach") as error:
        run(client.search_templates())
    assert error.value.__suppress_context__

    assert "unit-test-secret" not in str(error.value)
    assert "private-upstream-diagnostic" not in str(error.value)
    assert "unit-test-secret" not in caplog.text
    assert transport.closed


def test_cancellation_propagates_and_closes_session(monkeypatch):
    transport, _ = configure(monkeypatch, error=asyncio.CancelledError())
    client = docuseal_mcp.DocuSealMcpClient("https://mcp.example", "token")

    with pytest.raises(asyncio.CancelledError):
        run(client.search_templates())

    assert transport.closed

def test_send_cancellation_after_dispatch_is_unknown_without_retry(monkeypatch):
    transport, _ = configure(monkeypatch, error=asyncio.CancelledError())
    client = docuseal_mcp.DocuSealMcpClient("https://mcp.example", "unit-test-secret")

    with pytest.raises(docuseal_mcp.DocuSealMcpError) as error:
        run(
            client.send_documents(
                42, "signer@example.test", "Ada", "Signer", field_values=None
            )
        )

    assert "unknown" in str(error.value).lower()
    assert "inspect" in str(error.value).lower()
    assert "resend" in str(error.value).lower()
    assert "unit-test-secret" not in str(error.value)
    assert len(transport.session.calls) == 1
    assert transport.closed
def test_send_failure_is_outcome_unknown_without_retry_or_sensitive_details(
    monkeypatch, caplog
):
    transport, _ = configure(
        monkeypatch,
        error=RuntimeError("unit-test-secret private-upstream-diagnostic"),
    )
    client = docuseal_mcp.DocuSealMcpClient(
        "https://mcp.example", "unit-test-secret"
    )

    with pytest.raises(docuseal_mcp.DocuSealMcpError) as error:
        run(
            client.send_documents(
                42, "signer@example.test", "Ada", "Signer", {"Name": 23}
            )
        )

    message = str(error.value).lower()
    assert "unknown" in message
    assert "inspect" in message
    assert "resend" in message
    assert "unit-test-secret" not in str(error.value)
    assert "private-upstream-diagnostic" not in str(error.value)
    assert "unit-test-secret" not in caplog.text
    assert len(transport.session.calls) == 1
    assert transport.session.calls[0][0] == "send_documents"

def test_send_native_error_is_outcome_unknown_without_sensitive_details(monkeypatch):
    private_message = "private-upstream-diagnostic"
    transport, _ = configure(
        monkeypatch, result=result_with_text(private_message, is_error=True)
    )
    client = docuseal_mcp.DocuSealMcpClient("https://mcp.example", "unit-test-secret")

    with pytest.raises(docuseal_mcp.DocuSealMcpError) as error:
        run(
            client.send_documents(
                42, "signer@example.test", "Ada", "Signer", field_values=None
            )
        )

    message = str(error.value).lower()
    assert "unknown" in message
    assert "inspect" in message
    assert "resend" in message
    assert private_message not in str(error.value)
    assert "unit-test-secret" not in str(error.value)
    assert len(transport.session.calls) == 1
