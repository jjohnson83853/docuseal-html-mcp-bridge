"""Read-only native MCP client for DocuSeal template search."""

import asyncio
from ipaddress import ip_address
import json
from typing import Any, TypedDict
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


SEARCH_TIMEOUT_SECONDS = 20.0
MIN_SEARCH_LIMIT = 1
MAX_SEARCH_LIMIT = 100


class TemplateRecord(TypedDict):
    id: int
    name: str


class TemplateSearchResult(TypedDict):
    data: list[TemplateRecord]


class DocuSealMcpError(RuntimeError):
    """Safe error raised when native DocuSeal MCP search cannot complete."""


class DocuSealMcpClient:
    """Per-call Streamable HTTP client for native DocuSeal template search."""

    def __init__(self, url: str, bearer_token: str) -> None:
        self._url = self._validated_url(url)
        self._bearer_token = bearer_token

    @staticmethod
    def _validated_url(url: str) -> str:
        try:
            parsed = urlsplit(url)
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
            or "?" in url
            or "#" in url
            or not (
                parsed.scheme == "https"
                or (parsed.scheme == "http" and local_http)
            )
        ):
            raise ValueError(
                "DOCUSEAL_MCP_URL must use HTTPS without credentials, query, or "
                "fragment; HTTP is allowed only for localhost"
            )
        return url

    async def search_templates(
        self, query: str = "", limit: int = 10
    ) -> TemplateSearchResult:
        self._validate_limit(limit)
        if not self._bearer_token:
            raise DocuSealMcpError("DOCUSEAL_MCP_BEARER_TOKEN must be configured")

        result: Any = None
        transport_failed = False
        try:
            async with asyncio.timeout(SEARCH_TIMEOUT_SECONDS):
                async with httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {self._bearer_token}"},
                    timeout=SEARCH_TIMEOUT_SECONDS,
                ) as http_client:
                    async with streamable_http_client(
                        self._url, http_client=http_client
                    ) as (read_stream, write_stream, _):
                        async with ClientSession(read_stream, write_stream) as session:
                            await session.initialize()
                            result = await session.call_tool(
                                "search_templates", {"q": query, "limit": limit}
                            )
        except asyncio.CancelledError:
            raise
        except Exception:
            transport_failed = True

        if transport_failed:
            raise DocuSealMcpError("Could not reach DocuSeal MCP")
        return self._decode_result(result)

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not MIN_SEARCH_LIMIT <= limit <= MAX_SEARCH_LIMIT
        ):
            raise ValueError("limit must be between 1 and 100")

    @staticmethod
    def _decode_result(result: Any) -> TemplateSearchResult:
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
    def _valid_record(record: Any) -> bool:
        return (
            isinstance(record, dict)
            and isinstance(record.get("id"), int)
            and not isinstance(record.get("id"), bool)
            and isinstance(record.get("name"), str)
        )
