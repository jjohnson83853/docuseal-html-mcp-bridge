"""Native DocuSeal MCP client using one isolated Streamable HTTP session per call."""

import asyncio
from ipaddress import ip_address
import json
from typing import Any, TypedDict
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


MCP_TIMEOUT_SECONDS = 20.0
MIN_SEARCH_LIMIT = 1
MAX_SEARCH_LIMIT = 100
DEFAULT_DOCUSEAL_URL = "https://signature.bullprog.com"
SEND_OUTCOME_UNKNOWN_MESSAGE = (
    "The send outcome is unknown. Inspect DocuSeal before resending."
)


class TemplateRecord(TypedDict):
    id: int
    name: str


class TemplateSearchResult(TypedDict):
    data: list[TemplateRecord]


class DocuSealMcpError(RuntimeError):
    """Safe error raised when a native DocuSeal MCP call cannot complete."""


class DocuSealMcpClient:
    """Per-call Streamable HTTP client for documented DocuSeal MCP tools."""

    def __init__(self, url: str, bearer_token: str) -> None:
        self._url = self._validated_url(url)
        self._bearer_token = bearer_token

    @staticmethod
    def _validated_url(url: str) -> str:
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname
            port = parsed.port
        except (AttributeError, TypeError, ValueError):
            parsed = None
            hostname = None
            port = None

        local_http = False
        valid_hostname = False
        if hostname:
            try:
                local_http = ip_address(hostname).is_loopback
                valid_hostname = True
            except ValueError:
                labels = hostname.rstrip(".").split(".")
                valid_hostname = (
                    hostname.isascii()
                    and len(hostname.rstrip(".")) <= 253
                    and all(
                        1 <= len(label) <= 63
                        and label[0].isalnum()
                        and label[-1].isalnum()
                        and all(character.isalnum() or character == "-" for character in label)
                        for label in labels
                    )
                )
                local_http = hostname.lower() == "localhost"
        if (
            parsed is None
            or not valid_hostname
            or not parsed.netloc
            or port == 0
            or parsed.netloc.endswith(":")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or "?" in url
            or "#" in url
            or any(character.isspace() for character in url)
            or not (
                parsed.scheme == "https"
                or (parsed.scheme == "http" and local_http)
            )
        ):
            raise ValueError(
                "DOCUSEAL_URL must be an HTTPS root origin; HTTP is allowed only "
                "for loopback hosts"
            )
        origin = url.rstrip("/")
        return f"{origin}/mcp"

    async def search_templates(
        self, query: str = "", limit: int = 10
    ) -> TemplateSearchResult:
        self._validate_limit(limit)
        result = await self._call_tool("search_templates", {"q": query, "limit": limit})
        return self._decode_templates(result)

    async def load_template(self, template_id: int) -> dict[str, Any]:
        self._validate_id(template_id, "template_id")
        result = await self._call_tool("load_template", {"template_id": template_id})
        return self._native_result(result)

    async def search_documents(self, q: str = "", limit: int = 10) -> dict[str, Any]:
        self._validate_limit(limit)
        result = await self._call_tool("search_documents", {"q": q, "limit": limit})
        return self._native_result(result)

    async def send_documents(
        self,
        template_id: int,
        signer_email: str,
        signer_name: str,
        signer_role: str,
        field_values: dict[str, Any] | None,
    ) -> dict[str, Any]:
        self._validate_id(template_id, "template_id")
        submitter: dict[str, Any] = {
            "email": signer_email,
            "name": signer_name,
            "role": signer_role,
        }
        if field_values is not None:
            submitter["fields"] = [
                {"name": name, "value": value} for name, value in field_values.items()
            ]
        result = await self._call_tool(
            "send_documents",
            {"template_id": template_id, "submitters": [submitter]},
            outcome_unknown=True,
        )
        try:
            return self._native_result(result)
        except DocuSealMcpError:
            raise DocuSealMcpError(SEND_OUTCOME_UNKNOWN_MESSAGE) from None

    async def _call_tool(
        self, name: str, arguments: dict[str, Any], *, outcome_unknown: bool = False
    ) -> Any:
        if not self._bearer_token:
            raise DocuSealMcpError("DOCUSEAL_MCP_BEARER_TOKEN must be configured")

        dispatched = False
        try:
            async with asyncio.timeout(MCP_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {self._bearer_token}"},
                    timeout=MCP_TIMEOUT_SECONDS,
                ) as http_client:
                    async with streamable_http_client(
                        self._url, http_client=http_client
                    ) as (read_stream, write_stream, _):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            dispatched = True
                            return await session.call_tool(name, arguments)
        except asyncio.CancelledError:
            if outcome_unknown and dispatched:
                raise DocuSealMcpError(SEND_OUTCOME_UNKNOWN_MESSAGE) from None
            raise
        except Exception:
            if outcome_unknown and dispatched:
                raise DocuSealMcpError(SEND_OUTCOME_UNKNOWN_MESSAGE) from None
            raise DocuSealMcpError("Could not reach DocuSeal MCP") from None

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not MIN_SEARCH_LIMIT <= limit <= MAX_SEARCH_LIMIT
        ):
            raise ValueError("limit must be between 1 and 100")

    @staticmethod
    def _validate_id(value: int, name: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")

    @staticmethod
    def _decode_templates(result: Any) -> TemplateSearchResult:
        if getattr(result, "isError", None) is not False:
            raise DocuSealMcpError("DocuSeal MCP search failed")

        content = getattr(result, "content", None)
        if not isinstance(content, list) or len(content) != 1:
            raise DocuSealMcpError("DocuSeal MCP returned an invalid template list")
        block = content[0]
        if getattr(block, "type", None) != "text" or not isinstance(
            getattr(block, "text", None), str
        ):
            raise DocuSealMcpError("DocuSeal MCP returned an invalid template list")
        try:
            records = json.loads(block.text)
        except (TypeError, ValueError):
            raise DocuSealMcpError(
                "DocuSeal MCP returned an invalid template list"
            ) from None
        if not isinstance(records, list) or any(
            not DocuSealMcpClient._valid_record(record) for record in records
        ):
            raise DocuSealMcpError("DocuSeal MCP returned an invalid template list")
        return {
            "data": [{"id": record["id"], "name": record["name"]} for record in records]
        }

    @staticmethod
    def _native_result(result: Any) -> dict[str, Any]:
        if getattr(result, "isError", None) is not False:
            raise DocuSealMcpError("DocuSeal MCP operation failed")
        try:
            serialized = result.model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
            content = serialized["content"]
            if not isinstance(content, list):
                raise ValueError
            native: dict[str, Any] = {"content": content}
            if "structuredContent" in serialized:
                structured = serialized["structuredContent"]
                if not isinstance(structured, dict):
                    raise ValueError
                native["structuredContent"] = structured
            return native
        except Exception:
            raise DocuSealMcpError("DocuSeal MCP returned an invalid result") from None

    @staticmethod
    def _valid_record(record: Any) -> bool:
        return (
            isinstance(record, dict)
            and isinstance(record.get("id"), int)
            and not isinstance(record.get("id"), bool)
            and record["id"] > 0
            and isinstance(record.get("name"), str)
        )
