import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
import httpx

import server
from docuseal_api import DocuSealClient
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


def test_streamable_http_tools_call_docuseal_rest(monkeypatch):
    requests = []
    template = {
        "id": 42,
        "name": "Mutual NDA",
        "slug": "mutual-nda",
        "fields": [{"name": "Signer name", "type": "text"}],
    }
    submitter = {
        "email": "signer@example.test",
        "name": "Ada",
        "role": "Signer",
    }
    submission = {"id": 73, "template_id": 42, "status": "pending"}

    created_submitter = {
        **submitter,
        "id": 9,
        "submission_id": 73,
        "status": "pending",
    }

    def respond(request):
        requests.append(request)
        if request.url.path == "/api/templates":
            return httpx.Response(200, json={"data": [template], "pagination": {}})
        if request.url.path == "/api/templates/42":
            return httpx.Response(200, json=template)
        if request.url.path == "/api/submissions" and request.method == "POST":
            return httpx.Response(201, json=[created_submitter])
        if request.url.path == "/api/submissions":
            return httpx.Response(200, json={"data": [submission], "pagination": {}})
        if request.url.path == "/api/submissions/73":
            return httpx.Response(200, json={"id": 73, "status": "completed"})
        raise AssertionError(f"Unexpected upstream request: {request.method} {request.url}")

    api = DocuSealClient(
        base_url="https://signature.example",
        api_key="test-api-key",
        transport=httpx.MockTransport(respond),
    )
    monkeypatch.setattr(server, "_api_client", api)
    app = server.mcp.streamable_http_app()

    @asynccontextmanager
    async def client_factory(*, headers, timeout, auth):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers=headers,
            timeout=timeout,
            auth=auth,
        ) as client:
            yield client

    async def exercise():
        async with app.router.lifespan_context(app):
            async with streamablehttp_client(
                "http://bridge.test/mcp",
                httpx_client_factory=client_factory,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert {tool.name for tool in tools.tools} == {
                        "render_template_documents",
                        "search_templates",
                        "get_template",
                        "list_submissions",
                        "send_submission",
                        "get_submission_status",
                    }
                    render_tool = next(
                        tool for tool in tools.tools
                        if tool.name == "render_template_documents"
                    )
                    assert "manual" in render_tool.description.lower()

                    async def call(name, arguments):
                        result = await session.call_tool(name, arguments)
                        assert not result.isError, result
                        value = result.structuredContent
                        if value is None:
                            value = json.loads(result.content[0].text)
                        return value

                    found = await call("search_templates", {"query": "NDA"})
                    assert found["data"][0]["id"] == 42
                    assert found["data"][0]["fields"] == template["fields"]

                    loaded = await call("get_template", {"template_id": 42})
                    assert loaded["id"] == 42
                    assert loaded["fields"] == template["fields"]

                    submissions = await call(
                        "list_submissions",
                        {"template_id": 42, "status": "pending"},
                    )
                    submission_id = submissions["data"][0]["id"]
                    assert submission_id == 73

                    sent = await call(
                        "send_submission",
                        {
                            "template_id": 42,
                            "signer_email": "signer@example.test",
                            "signer_name": "Ada",
                        },
                    )
                    assert sent["submission_id"] == 73

                    status = await call(
                        "get_submission_status", {"submission_id": submission_id}
                    )
                    assert status["id"] == submission_id
                    assert status["status"] == "completed"

    asyncio.run(exercise())
    assert [request.url.path for request in requests] == [
        "/api/templates",
        "/api/templates/42",
        "/api/submissions",
        "/api/submissions",
        "/api/submissions/73",
    ]
    assert all(request.headers["X-Auth-Token"] == "test-api-key" for request in requests)
    assert dict(requests[0].url.params) == {"q": "NDA", "limit": "10"}
    assert dict(requests[2].url.params) == {"template_id": "42", "status": "pending", "limit": "10"}
    assert json.loads(requests[3].content) == {
        "template_id": 42,
        "submitters": [
            {
                "email": "signer@example.test",
                "name": "Ada",
                "role": "Signer",
            }
        ],
    }



def test_server_starts_without_docuseal_api_key(monkeypatch):

    monkeypatch.setattr(server, "DOCUSEAL_API_KEY", "")
    monkeypatch.delenv("DOCUSEAL_API_KEY", raising=False)
    started = []

    async def run_servers(config):
        started.append(config)

    monkeypatch.setattr(server, "run_servers", run_servers)

    server.main()

    assert len(started) == 1
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

def test_mcp_render_tool_works_without_rest_key_or_upstream_requests(monkeypatch, tmp_path):
    import document_export

    async def render_without_external_pagination(*args, **kwargs):
        return b"%PDF fixture", b"PK\x03\x04DOCX fixture"

    monkeypatch.setattr(
        document_export,
        "_subprocess_worker",
        render_without_external_pagination,
    )
    from mcp.server.fastmcp import FastMCP

    requests = []

    def reject_upstream(request):
        requests.append(request)
        raise AssertionError("offline rendering attempted a DocuSeal request")

    monkeypatch.setattr(server, "DOCUSEAL_API_KEY", "")
    monkeypatch.setenv("DOCUSEAL_API_KEY", "")
    monkeypatch.setenv("ARTIFACT_PUBLIC_BASE_URL", "https://downloads.example.test")
    monkeypatch.setattr(server, "_artifact_store", server.ArtifactStore(tmp_path))
    monkeypatch.setattr(
        server,
        "_api_client",
        DocuSealClient(
            base_url="https://signature.example",
            api_key="",
            transport=httpx.MockTransport(reject_upstream),
        ),
    )
    test_mcp = FastMCP("Offline render test", streamable_http_path="/mcp")
    test_mcp.add_tool(server.render_template_documents)
    app = test_mcp.streamable_http_app()

    @asynccontextmanager
    async def client_factory(*, headers, timeout, auth):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            headers=headers,
            timeout=timeout,
            auth=auth,
        ) as client:
            yield client

    async def exercise():
        async with app.router.lifespan_context(app):
            async with streamablehttp_client(
                "http://bridge.test/mcp",
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

    assert requests == []
