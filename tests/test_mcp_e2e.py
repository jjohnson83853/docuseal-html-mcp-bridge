import asyncio
import json
from contextlib import asynccontextmanager

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
        if request.url.path == "/api/v1/templates/html":
            return httpx.Response(201, json=template)
        if request.url.path == "/api/v1/templates":
            return httpx.Response(200, json={"data": [template], "pagination": {}})
        if request.url.path == "/api/v1/templates/42":
            return httpx.Response(200, json=template)
        if request.url.path == "/api/v1/submissions" and request.method == "POST":
            return httpx.Response(201, json=[created_submitter])
        if request.url.path == "/api/v1/submissions":
            return httpx.Response(200, json={"data": [submission], "pagination": {}})
        if request.url.path == "/api/v1/submissions/73":
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
                        "create_template_from_html",
                        "search_templates",
                        "get_template",
                        "list_submissions",
                        "send_submission",
                        "get_submission_status",
                    }

                    async def call(name, arguments):
                        result = await session.call_tool(name, arguments)
                        assert not result.isError, result
                        value = result.structuredContent
                        if value is None:
                            value = json.loads(result.content[0].text)
                        return value

                    created = await call(
                        "create_template_from_html",
                        {
                            "template_name": "Mutual NDA",
                            "html_content": '<text-field name="Signer name" role="Signer"></text-field>',
                            "document_size": "Letter",
                        },
                    )
                    assert created["id"] == 42
                    assert created["fields"] == template["fields"]

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
        "/api/v1/templates/html",
        "/api/v1/templates",
        "/api/v1/templates/42",
        "/api/v1/submissions",
        "/api/v1/submissions",
        "/api/v1/submissions/73",
    ]
    assert all(request.headers["X-Auth-Token"] == "test-api-key" for request in requests)
    assert json.loads(requests[0].content) == {
        "name": "Mutual NDA",
        "html": '<text-field name="Signer name" role="Signer"></text-field>',
        "size": "Letter",
    }
    assert dict(requests[1].url.params) == {"q": "NDA", "limit": "10"}
    assert dict(requests[3].url.params) == {"template_id": "42", "status": "pending", "limit": "10"}
    assert json.loads(requests[4].content) == {
        "template_id": 42,
        "submitters": [
            {
                "email": "signer@example.test",
                "name": "Ada",
                "role": "Signer",
            }
        ],
    }


def test_server_requires_docuseal_api_key_before_starting(monkeypatch):
    monkeypatch.setattr(server, "DOCUSEAL_API_KEY", "")

    with pytest.raises(RuntimeError, match="DOCUSEAL_API_KEY"):
        server.main()
