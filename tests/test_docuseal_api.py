import asyncio
import json

import httpx
import pytest

import docuseal_api
from docuseal_api import (
    DocuSealAPIError,
    DocuSealClient,
    SubmissionOutcomeUnknown,
)


BASE_URL = "https://signature.example"
API_KEY = "test-api-key"


def run(coroutine):
    return asyncio.run(coroutine)


def make_client(handler):
    transport = httpx.MockTransport(handler)
    return DocuSealClient(
        base_url=f"{BASE_URL}/",
        api_key=API_KEY,
        transport=transport,
    )


def test_search_templates_sends_query_and_pagination_and_returns_records():
    response_payload = {
        "data": [{"id": 17, "name": "Mutual NDA", "fields": [{"name": "Signer"}]}],
        "pagination": {"count": 1, "next": None},
    }
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=response_payload)

    client = make_client(respond)
    result = run(client.search_templates(query="NDA", limit=25))

    assert result["data"][0]["id"] == 17
    assert result["data"][0]["fields"] == [{"name": "Signer"}]
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/api/v1/templates"
    assert dict(request.url.params) == {"q": "NDA", "limit": "25"}
    assert request.headers["X-Auth-Token"] == API_KEY


def test_create_template_posts_tagged_html_and_returns_template_details():
    html = '<text-field name="legal name" role="Signer" required></text-field>\n雪'
    template = {
        "id": 42,
        "slug": "mutual-nda",
        "name": "Mutual NDA",
        "fields": [{"name": "legal name", "type": "text"}],
        "created_at": "2026-09-29T00:00:00Z",
    }
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(201, json=template)

    client = make_client(respond)
    result = run(client.create_template_from_html("Mutual NDA", html, "A4"))

    assert result["id"] == 42
    assert result["fields"] == template["fields"]
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/api/v1/templates/html"
    assert json.loads(request.content) == {"name": "Mutual NDA", "html": html, "size": "A4"}
    assert request.headers["X-Auth-Token"] == API_KEY


def test_get_template_uses_documented_detail_route_and_returns_id_fields():
    template = {"id": 42, "name": "Mutual NDA", "fields": [{"name": "Signer"}]}
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=template)

    result = run(make_client(respond).get_template(42))

    assert result["id"] == 42
    assert result["fields"] == template["fields"]
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/v1/templates/42"


def test_list_submissions_maps_supported_filters_to_v1_list_route():
    payload = {"data": [{"id": 9, "template_id": 42, "status": "completed"}], "pagination": {}}
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=payload)

    result = run(
        make_client(respond).list_submissions(
            template_id=42, status="completed", query="NDA", limit=10
        )
    )

    assert result["data"][0]["id"] == 9
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/v1/submissions"
    assert dict(seen[0].url.params) == {
        "template_id": "42",
        "status": "completed",
        "q": "NDA",
        "limit": "10",
    }


def test_send_submission_posts_signer_data_and_returns_submission_id():
    payload = [
        {
            "id": 2,
            "submission_id": 73,
            "status": "pending",
            "email": "signer@example.test",
            "name": "Ada",
            "role": "Signer",
        }
    ]
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(201, json=payload)

    result = run(
        make_client(respond).send_submission(
            template_id=42,
            signer_email="signer@example.test",
            signer_name="Ada",
            signer_role="Signer",
            field_values={"legal name": "Ada"},
        )
    )

    assert result["submission_id"] == 73
    assert seen[0].method == "POST"
    assert seen[0].url.path == "/api/v1/submissions"
    assert json.loads(seen[0].content) == {
        "template_id": 42,
        "submitters": [
            {
                "email": "signer@example.test",
                "name": "Ada",
                "role": "Signer",
                "values": {"legal name": "Ada"},
            }
        ],
    }


def test_submission_status_uses_id_detail_route_and_returns_status():
    submission = {"id": 73, "status": "completed", "template_id": 42}
    seen = []

    def respond(request):
        seen.append(request)
        return httpx.Response(200, json=submission)

    result = run(make_client(respond).get_submission_status(73))

    assert result["id"] == 73
    assert result["status"] == "completed"
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/api/v1/submissions/73"


def test_http_error_is_propagated_without_exposing_api_key_or_document_html():
    api_key = "secret-test-token"
    document_html = "<text-field name='private'></text-field>"

    def respond(request):
        return httpx.Response(500, text=f"upstream failure: {api_key} {document_html}")

    client = DocuSealClient(
        base_url=BASE_URL,
        api_key=api_key,
        transport=httpx.MockTransport(respond),
    )

    with pytest.raises(DocuSealAPIError) as error:
        run(client.search_templates())

    assert "500" in str(error.value)
    assert api_key not in str(error.value)
    assert document_html not in str(error.value)


def test_missing_api_key_fails_clearly_before_sending_request():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    client = DocuSealClient(
        base_url=BASE_URL,
        api_key="",
        transport=httpx.MockTransport(respond),
    )

    with pytest.raises(DocuSealAPIError, match="DOCUSEAL_API_KEY"):
        run(client.search_templates())

    assert requests == []


@pytest.mark.parametrize("payload", [{}, [], {"id": None}])
def test_template_detail_rejects_responses_without_a_template_id(payload):
    def respond(request):
        return httpx.Response(200, json=payload)

    with pytest.raises(DocuSealAPIError, match="invalid template response"):
        run(make_client(respond).get_template(42))


@pytest.mark.parametrize("payload", [{}, [], [{"status": "pending"}]])
def test_submission_creation_rejects_responses_without_submission_identity(payload):
    def respond(request):
        return httpx.Response(201, json=payload)

    with pytest.raises(DocuSealAPIError):
        run(
            make_client(respond).send_submission(
                template_id=42,
                signer_email="signer@example.test",
                signer_name="Ada",
            )
        )


@pytest.mark.parametrize("limit", [0, 101])
def test_template_search_rejects_limits_outside_api_bounds(limit):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    with pytest.raises(ValueError, match="between 1 and 100"):
        run(make_client(respond).search_templates(limit=limit))
    assert requests == []


def test_template_search_rejects_malformed_list_response():
    def respond(request):
        return httpx.Response(200, json={"data": {}})

    with pytest.raises(DocuSealAPIError, match="invalid templates list"):
        run(make_client(respond).search_templates())


def test_docuseal_connection_error_does_not_expose_request_details():
    secret = "private-connection-detail"

    def respond(request):
        raise httpx.ConnectError(secret, request=request)

    with pytest.raises(DocuSealAPIError, match="Could not reach the DocuSeal API") as error:
        run(make_client(respond).search_templates())
    assert secret not in str(error.value)


def test_non_json_docuseal_response_is_rejected():
    def respond(request):
        return httpx.Response(200, text="not-json")

    with pytest.raises(DocuSealAPIError, match="invalid JSON response"):
        run(make_client(respond).search_templates())


def test_empty_detail_response_is_rejected_as_missing_template():
    def respond(request):
        return httpx.Response(204)

    with pytest.raises(DocuSealAPIError, match="invalid template response"):
        run(make_client(respond).get_template(42))


@pytest.mark.parametrize(
    "base_url",
    [
        "http://signature.example",
        "file:///tmp/docuseal",
        "https://user:secret@signature.example",
        "https://signature.example/?token=embedded",
    ],
)
def test_client_rejects_untrusted_or_insecure_base_urls(base_url):
    with pytest.raises(ValueError, match="DOCUSEAL_URL"):
        DocuSealClient(base_url=base_url, api_key=API_KEY)


@pytest.mark.parametrize("template_id", [0, -1, True, "42/../../submissions"])
def test_template_detail_rejects_invalid_ids_before_request(template_id):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"id": 42})

    with pytest.raises(ValueError, match="template_id"):
        run(make_client(respond).get_template(template_id))
    assert requests == []


def test_submission_timeout_warns_not_to_retry_without_checking_first():
    detail = "private transport detail"

    def respond(request):
        raise httpx.ReadTimeout(detail, request=request)

    with pytest.raises(SubmissionOutcomeUnknown) as error:
        run(
            make_client(respond).send_submission(
                template_id=42,
                signer_email="signer@example.test",
                signer_name="Ada",
            )
        )
    assert "may have been created" in str(error.value)
    assert "check docuseal submissions before retrying" in str(error.value).lower()
    assert detail not in str(error.value)


def test_failed_submission_response_warns_that_request_may_exist():
    def respond(request):
        return httpx.Response(500, text="private upstream response")

    with pytest.raises(SubmissionOutcomeUnknown, match="may have been created"):
        run(
            make_client(respond).send_submission(
                template_id=42,
                signer_email="signer@example.test",
                signer_name="Ada",
            )
        )


def test_loopback_http_is_allowed_for_local_docuseal_development():
    def respond(request):
        assert request.url.scheme == "http"
        return httpx.Response(200, json={"data": []})

    result = run(
        DocuSealClient(
            base_url="http://127.0.0.1:3000",
            api_key=API_KEY,
            transport=httpx.MockTransport(respond),
        ).search_templates()
    )
    assert result["data"] == []


def test_malformed_docuseal_url_port_fails_configuration_validation():
    with pytest.raises(ValueError, match="DOCUSEAL_URL"):
        DocuSealClient(base_url="https://signature.example:not-a-port")


def test_non_connect_read_error_uses_safe_failure_message():
    detail = "private read failure"

    def respond(request):
        raise httpx.ReadError(detail, request=request)

    with pytest.raises(DocuSealAPIError, match="Could not reach the DocuSeal API") as error:
        run(make_client(respond).search_templates())
    assert detail not in str(error.value)


def test_non_json_submission_response_warns_of_unknown_outcome():
    def respond(request):
        return httpx.Response(201, text="private malformed response")

    with pytest.raises(SubmissionOutcomeUnknown, match="may have been created"):
        run(
            make_client(respond).send_submission(
                template_id=42,
                signer_email="signer@example.test",
                signer_name="Ada",
            )
        )


@pytest.mark.parametrize("limit", [0, 101])
def test_submission_listing_rejects_limits_outside_api_bounds(limit):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"data": []})

    with pytest.raises(ValueError, match="between 1 and 100"):
        run(make_client(respond).list_submissions(limit=limit))
    assert requests == []


@pytest.mark.parametrize("submission_id", [True, "73/../../templates"])
def test_submission_creation_rejects_invalid_returned_ids(submission_id):
    def respond(request):
        return httpx.Response(201, json=[{"submission_id": submission_id}])

    with pytest.raises(SubmissionOutcomeUnknown, match="may have been created"):
        run(
            make_client(respond).send_submission(
                template_id=42,
                signer_email="signer@example.test",
                signer_name="Ada",
            )
        )


def test_submission_redirect_is_reported_as_unknown_outcome():
    def respond(request):
        return httpx.Response(302, headers={"Location": "/api/v1/submissions/73"})

    with pytest.raises(SubmissionOutcomeUnknown, match="may have been created"):
        run(
            make_client(respond).send_submission(
                template_id=42,
                signer_email="signer@example.test",
                signer_name="Ada",
            )
        )


def test_submission_total_deadline_preserves_unknown_outcome_warning(monkeypatch):
    class DelayedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            await asyncio.sleep(0.05)
            return httpx.Response(201, json=[{"submission_id": 73}])

        async def aclose(self):
            pass

    monkeypatch.setattr(docuseal_api, "SUBMISSION_DEADLINE_SECONDS", 0.01, raising=False)
    client = DocuSealClient(
        base_url=BASE_URL,
        api_key=API_KEY,
        transport=DelayedTransport(),
    )

    with pytest.raises(SubmissionOutcomeUnknown, match="may have been created"):
        run(
            client.send_submission(
                template_id=42,
                signer_email="signer@example.test",
                signer_name="Ada",
            )
        )
