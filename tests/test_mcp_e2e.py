import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import server
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, TextContent



def test_streamable_http_bridge_uses_only_retained_native_mcp_tools(monkeypatch):
    import docuseal_mcp

    native_calls = []
    native_results = {
        "search_templates": [{"id": 42, "name": "Mutual NDA"}],
        "load_template": {"id": 42, "name": "Mutual NDA", "fields": []},
        "search_documents": {"documents": [{"id": 73, "status": "completed"}]},
        "send_documents": {"documents": [{"id": 74, "status": "pending"}]},
    }

    class UpstreamSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def initialize(self):
            return None

        async def call_tool(self, name, arguments):
            native_calls.append((name, arguments))
            result = native_results[name]
            return CallToolResult(
                content=[TextContent(type="text", text=json.dumps(result))],
                structuredContent=(
                    None if name == "search_templates" else result
                ),
            )

    @asynccontextmanager
    async def upstream_transport(url, *, http_client):
        assert url == "https://native.example/mcp"
        assert http_client.headers["Authorization"] == "Bearer native-token"
        yield object(), object(), "session"

    monkeypatch.setattr(docuseal_mcp, "streamable_http_client", upstream_transport)
    monkeypatch.setattr(docuseal_mcp, "ClientSession", lambda *args: UpstreamSession())
    monkeypatch.setattr(
        server,
        "_mcp_client",
        docuseal_mcp.DocuSealMcpClient("https://native.example", "native-token"),
    )
    app = server.mcp.streamable_http_app()

    def client_factory(*, headers, timeout, auth):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers=headers,
            timeout=timeout,
            auth=auth,
        )

    async def exercise():
        async with app.router.lifespan_context(app):
            async with streamablehttp_client(
                "http://127.0.0.1:8000/mcp",
                httpx_client_factory=client_factory,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert {tool.name for tool in tools.tools} == {
                        "render_template_documents",
                        "search_templates",
                        "get_template",
                        "send_submission",
                        "search_documents",
                    }

                    async def call(name, arguments):
                        result = await session.call_tool(name, arguments)
                        assert not result.isError, (result, native_calls)
                        value = result.structuredContent
                        if value is None:
                            value = json.loads(result.content[0].text)
                        return value

                    found = await call(
                        "search_templates", {"query": "Mutual NDA", "limit": 12}
                    )
                    assert found["data"] == native_results["search_templates"]
                    loaded = await call("get_template", {"template_id": 42})
                    assert loaded["structuredContent"] == native_results["load_template"]
                    documents = await call(
                        "search_documents", {"q": "completed", "limit": 8}
                    )
                    assert documents["structuredContent"] == native_results["search_documents"]
                    assert documents["content"][0]["text"] == json.dumps(
                        native_results["search_documents"]
                    )
                    with_fields = await call(
                        "send_submission",
                        {
                            "template_id": 42,
                            "signer_email": "signer@example.test",
                            "signer_name": "Ada",
                            "signer_role": "Reviewer",
                            "field_values": {"Signer name": 19},
                        },
                    )
                    assert with_fields["structuredContent"] == native_results["send_documents"]
                    without_fields = await call(
                        "send_submission",
                        {
                            "template_id": 42,
                            "signer_email": "other@example.test",
                            "signer_name": "Lin",
                            "field_values": None,
                        },
                    )
                    assert without_fields["structuredContent"] == native_results["send_documents"]

    asyncio.run(exercise())
    assert native_calls == [
        ("search_templates", {"q": "Mutual NDA", "limit": 12}),
        ("load_template", {"template_id": 42}),
        ("search_documents", {"q": "completed", "limit": 8}),
        (
            "send_documents",
            {
                "template_id": 42,
                "submitters": [{
                    "email": "signer@example.test",
                    "name": "Ada",
                    "role": "Reviewer",
                    "fields": [{"name": "Signer name", "value": 19}],
                }],
            },
        ),
        (
            "send_documents",
            {
                "template_id": 42,
                "submitters": [{
                    "email": "other@example.test",
                    "name": "Lin",
                    "role": "Signer",
                }],
            },
        ),
    ]

def test_server_starts_without_mcp_bearer_token(monkeypatch):

    monkeypatch.setattr(server, "DOCUSEAL_MCP_BEARER_TOKEN", "")
    monkeypatch.delenv("DOCUSEAL_MCP_BEARER_TOKEN", raising=False)
    started = []

    async def run_servers(config):
        started.append(config)

    monkeypatch.setattr(server, "run_servers", run_servers)

    server.main()

    assert len(started) == 1
@pytest.mark.parametrize(
    "origin",
    [
        "http://signature.example",
        "https://signature.example/mcp",
        "https://user:secret@signature.example",
        "https://signature.example?",
        "https://signature.example:65536",
    ],
)
def test_invalid_docuseal_origin_fails_before_server_starts(monkeypatch, origin):
    monkeypatch.setenv("DOCUSEAL_URL", origin)
    started = []

    async def run_servers(config):
        started.append(config)

    monkeypatch.setattr(server, "run_servers", run_servers)

    with pytest.raises(ValueError, match="DOCUSEAL_URL"):
        server.main()

    assert started == []
@pytest.mark.parametrize("port", ["8000", "1023", "65536", "not-a-port", ""])
def test_invalid_artifact_port_fails_before_any_listener_starts(monkeypatch, port):
    monkeypatch.setenv("ARTIFACT_PORT", port)
    started = []

    async def run_servers(config):
        started.append(config)

    monkeypatch.setattr(server, "run_servers", run_servers)

    with pytest.raises((ValueError, RuntimeError), match="ARTIFACT_PORT"):
        server.main()

    assert started == []


def test_artifact_port_accepts_configured_nondefault_value():
    config = server.load_config({"ARTIFACT_PORT": "9443"})

    assert config.artifact_port == 9443


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"MAX_INPUT_BYTES": "invalid"}, "MAX_INPUT_BYTES"),
        ({"MAX_INPUT_BYTES": "0"}, "MAX_INPUT_BYTES"),
        ({"WORKER_TIMEOUT_SECONDS": "invalid"}, "WORKER_TIMEOUT_SECONDS"),
        ({"WORKER_TIMEOUT_SECONDS": "nan"}, "WORKER_TIMEOUT_SECONDS"),
        ({"MAX_CONCURRENT_RENDERS": "2"}, "MAX_CONCURRENT_RENDERS"),
        ({"ARTIFACT_RETENTION_SECONDS": "86401"}, "ARTIFACT_RETENTION_SECONDS"),
        ({"ARTIFACT_DIR": ""}, "ARTIFACT_DIR"),
        ({"ARTIFACT_DIR": str(Path.cwd())}, "ARTIFACT_DIR"),
    ],
)
def test_server_rejects_unsafe_artifact_configuration(values, message):
    with pytest.raises(ValueError, match=message):
        server.load_config(values)


def test_server_defers_artifact_store_until_runtime_configuration():
    assert server._artifact_store is None
def test_mcp_listener_does_not_serve_artifact_download_route():
    app = server.mcp.streamable_http_app()

    async def exercise():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://bridge.test",
        ) as client:
            return await client.get("/download?token=synthetic&format=pdf")

    response = asyncio.run(exercise())

    assert response.status_code == 404

def test_mcp_render_tool_works_without_mcp_token_or_upstream_calls(monkeypatch, tmp_path):
    import document_export

    async def render_without_external_pagination(*args, **kwargs):
        return b"%PDF fixture", b"PK\x03\x04DOCX fixture"

    monkeypatch.setattr(
        document_export,
        "_subprocess_worker",
        render_without_external_pagination,
    )
    from mcp.server.fastmcp import FastMCP

    monkeypatch.setattr(server, "DOCUSEAL_MCP_BEARER_TOKEN", "")
    monkeypatch.setenv("DOCUSEAL_MCP_BEARER_TOKEN", "")
    monkeypatch.setenv("ARTIFACT_PUBLIC_BASE_URL", "https://downloads.example.test")
    monkeypatch.setattr(server, "_artifact_store", server.ArtifactStore(tmp_path))
    monkeypatch.setattr(server, "_mcp_client", object())
    test_mcp = FastMCP("Offline render test", streamable_http_path="/mcp")
    test_mcp.add_tool(server.render_template_documents)
    app = test_mcp.streamable_http_app()

    def client_factory(*, headers, timeout, auth):
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers=headers,
            timeout=timeout,
            auth=auth,
        )

    async def exercise():
        async with app.router.lifespan_context(app):
            async with streamablehttp_client(
                "http://127.0.0.1:8000/mcp",
                httpx_client_factory=client_factory,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "render_template_documents",
                        {
                            "template_name": "Offline draft",
                            "html_content": '<text-field name="Signer name" role="Signer"></text-field>',
                        },
                    )
                    assert not result.isError, result
                    value = result.structuredContent
                    if value is None:
                        value = json.loads(result.content[0].text)
                    assert value["pdf_url"].startswith(
                        "https://downloads.example.test/download?"
                    )
                    assert value["docx_url"].startswith(
                        "https://downloads.example.test/download?"
                    )
    asyncio.run(exercise())
    assert server._artifact_store.root == tmp_path



