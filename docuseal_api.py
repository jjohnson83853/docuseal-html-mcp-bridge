"""Async REST client for the DocuSeal API used by the MCP bridge."""

import asyncio

from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

import httpx


class DocuSealAPIError(RuntimeError):
    """Safe, user-facing error from a DocuSeal API operation."""


SUBMISSION_DEADLINE_SECONDS = 20.0


class SubmissionOutcomeUnknown(DocuSealAPIError):
    """Upstream may have accepted a signing request without confirming its ID."""

class DocuSealClient:
    """Small REST adapter with an injectable transport for deterministic tests."""

    def __init__(
        self,
        base_url: str = "https://signature.bullprog.com",
        api_key: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = self._validated_base_url(base_url)
        self._api_key = api_key
        self._transport = transport

    async def search_templates(
        self, query: str = "", limit: int = 10
    ) -> dict[str, Any]:
        self._validate_limit(limit)
        params: dict[str, Any] = {"limit": limit}
        if query:
            params["q"] = query
        payload = await self._request("GET", "templates", params=params)
        return self._list_response(payload, "templates")

    async def create_template_from_html(
        self, template_name: str, html_content: str, document_size: str = "Letter"
    ) -> dict[str, Any]:
        payload = await self._request(
            "POST",
            "templates/html",
            json={"name": template_name, "html": html_content, "size": document_size},
        )
        return self._object_response(payload, "template")

    async def get_template(self, template_id: int) -> dict[str, Any]:
        self._validate_id(template_id, "template_id")
        payload = await self._request("GET", f"templates/{template_id}")
        return self._object_response(payload, "template")

    async def list_submissions(
        self,
        template_id: int | None = None,
        status: str | None = None,
        query: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        self._validate_limit(limit)
        if template_id is not None:
            self._validate_id(template_id, "template_id")
        params: dict[str, Any] = {"limit": limit}
        if template_id is not None:
            params["template_id"] = template_id
        if status:
            params["status"] = status
        if query:
            params["q"] = query
        payload = await self._request("GET", "submissions", params=params)
        return self._list_response(payload, "submissions")

    async def send_submission(
        self,
        template_id: int,
        signer_email: str,
        signer_name: str,
        signer_role: str = "Signer",
        field_values: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._validate_id(template_id, "template_id")
        submitter: dict[str, Any] = {
            "email": signer_email,
            "name": signer_name,
            "role": signer_role,
        }
        if field_values is not None:
            submitter["values"] = field_values
        try:
            async with asyncio.timeout(SUBMISSION_DEADLINE_SECONDS):
                payload = await self._request(
                    "POST",
                    "submissions",
                    ambiguous_write=True,
                    json={"template_id": template_id, "submitters": [submitter]},
                )
                if (
                    not isinstance(payload, list)
                    or not payload
                    or not isinstance(payload[0], dict)
                ):
                    raise self._submission_outcome_unknown()
                submission_id = payload[0].get("submission_id")
                try:
                    self._validate_id(submission_id, "submission_id")
                except ValueError:
                    raise self._submission_outcome_unknown() from None
        except TimeoutError:
            raise self._submission_outcome_unknown() from None
        return {"submission_id": submission_id, "submitters": payload}

    async def get_submission_status(self, submission_id: int) -> dict[str, Any]:
        self._validate_id(submission_id, "submission_id")
        payload = await self._request("GET", f"submissions/{submission_id}")
        return self._object_response(payload, "submission")

    async def _request(
        self, method: str, path: str, *, ambiguous_write: bool = False, **kwargs: Any
    ) -> Any:
        if not self._api_key:
            raise DocuSealAPIError("DOCUSEAL_API_KEY must be configured")
        url = f"{self._base_url}/{path.lstrip('/')}"
        try:
            async with httpx.AsyncClient(
                timeout=30.0, transport=self._transport
            ) as client:
                response = await client.request(
                    method,
                    url,
                    headers={"X-Auth-Token": self._api_key},
                    **kwargs,
                )
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            status = error.response.status_code
            if ambiguous_write and (300 <= status < 400 or status >= 500):
                raise self._submission_outcome_unknown() from None
            raise DocuSealAPIError(
                f"DocuSeal API request failed with HTTP {status}"
            ) from None
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise DocuSealAPIError("Could not reach the DocuSeal API") from None
        except httpx.RequestError:
            if ambiguous_write:
                raise self._submission_outcome_unknown() from None
            raise DocuSealAPIError("Could not reach the DocuSeal API") from None

        if not response.content:
            if ambiguous_write:
                raise self._submission_outcome_unknown()
            return {}
        try:
            return response.json()
        except ValueError:
            if ambiguous_write:
                raise self._submission_outcome_unknown() from None
            raise DocuSealAPIError("DocuSeal returned an invalid JSON response") from None

    @staticmethod
    def _validated_base_url(base_url: str) -> str:
        try:
            parsed = urlsplit(base_url)
            hostname = parsed.hostname
            parsed.port
        except ValueError:
            hostname = None
            parsed = None

        local_http = False
        if hostname:
            local_http = hostname.lower() == "localhost"
            if not local_http:
                try:
                    local_http = ip_address(hostname).is_loopback
                except ValueError:
                    pass

        if (
            parsed is None
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not (parsed.scheme == "https" or (parsed.scheme == "http" and local_http))
        ):
            raise ValueError(
                "DOCUSEAL_URL must use HTTPS without credentials, query, or fragment; "
                "HTTP is allowed only for localhost"
            )
        normalized_url = base_url.rstrip("/")
        if parsed.path.rstrip("/").endswith("/api"):
            return normalized_url
        return f"{normalized_url}/api"

    @staticmethod
    def _validate_id(resource_id: int, name: str) -> None:
        if isinstance(resource_id, bool) or not isinstance(resource_id, int) or resource_id <= 0:
            raise ValueError(f"{name} must be a positive integer")

    @staticmethod
    def _submission_outcome_unknown() -> SubmissionOutcomeUnknown:
        return SubmissionOutcomeUnknown(
            "Submission outcome is unknown; it may have been created. "
            "Check DocuSeal submissions before retrying."
        )

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")

    @staticmethod
    def _list_response(payload: Any, resource: str) -> dict[str, Any]:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise DocuSealAPIError(f"DocuSeal returned an invalid {resource} list")
        return payload

    @staticmethod
    def _object_response(payload: Any, resource: str) -> dict[str, Any]:
        if not isinstance(payload, dict) or payload.get("id") is None:
            raise DocuSealAPIError(f"DocuSeal returned an invalid {resource} response")
        return payload
