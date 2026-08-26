"""Async OpenSearch client + index lifecycle.

Index naming uses a versioned physical index behind a stable alias
(`kb-chunks` -> `kb-chunks-v3`) so a re-embed can be built in the background
and swapped atomically with zero read downtime.
"""
from __future__ import annotations

from typing import Any

from opensearchpy import AsyncHttpConnection, AsyncOpenSearch
from opensearchpy.exceptions import NotFoundError as OSNotFound

from app.core.config import settings
from app.core.exceptions import VectorStoreError
from app.core.logging import get_logger
from app.vector.index_mappings import build_index_body

log = get_logger(__name__)

_client: AsyncOpenSearch | None = None


def get_client() -> AsyncOpenSearch:
    global _client
    if _client is None:
        auth = (
            (settings.opensearch_user, settings.opensearch_password)
            if settings.opensearch_user
            else None
        )
        _client = AsyncOpenSearch(
            hosts=settings.opensearch_hosts,
            http_auth=auth,
            use_ssl=any(h.startswith("https") for h in settings.opensearch_hosts),
            verify_certs=settings.opensearch_verify_certs,
            ssl_show_warn=False,
            connection_class=AsyncHttpConnection,
            timeout=30,
            max_retries=3,
            retry_on_timeout=True,
            pool_maxsize=50,
        )
    return _client


async def close_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None


async def ping() -> bool:
    try:
        return bool(await get_client().ping())
    except Exception as exc:  # noqa: BLE001
        log.warning("opensearch.ping_failed", error=str(exc))
        return False


async def resolve_alias(alias: str | None = None) -> str:
    """Return the physical index currently behind the alias."""
    alias = alias or settings.opensearch_index_alias
    try:
        response = await get_client().indices.get_alias(name=alias)
        return next(iter(response.keys()))
    except OSNotFound:
        return f"{settings.opensearch_index_prefix}1"


async def ensure_index(version: int = 1, *, alias: str | None = None) -> str:
    """Create the versioned index and point the alias at it if absent."""
    client = get_client()
    alias = alias or settings.opensearch_index_alias
    index = f"{settings.opensearch_index_prefix}{version}"
    try:
        if not await client.indices.exists(index=index):
            await client.indices.create(index=index, body=build_index_body())
            log.info("opensearch.index_created", index=index)
        if not await client.indices.exists_alias(name=alias):
            await client.indices.put_alias(index=index, name=alias)
            log.info("opensearch.alias_created", alias=alias, index=index)
    except Exception as exc:  # noqa: BLE001
        raise VectorStoreError(f"Failed to prepare index {index}: {exc}") from exc
    return index


async def swap_alias(new_index: str, *, alias: str | None = None) -> None:
    """Atomically repoint the alias -- used at the end of a re-index."""
    alias = alias or settings.opensearch_index_alias
    client = get_client()
    actions: list[dict[str, Any]] = []
    try:
        current = await client.indices.get_alias(name=alias)
        actions += [{"remove": {"index": idx, "alias": alias}} for idx in current]
    except OSNotFound:
        pass
    actions.append({"add": {"index": new_index, "alias": alias}})
    await client.indices.update_aliases(body={"actions": actions})
    log.info("opensearch.alias_swapped", alias=alias, index=new_index)


async def index_stats(index: str | None = None) -> dict[str, Any]:
    index = index or settings.opensearch_index_alias
    try:
        stats = await get_client().indices.stats(index=index)
        primaries = stats["_all"]["primaries"]
        return {
            "index": index,
            "docs": primaries["docs"]["count"],
            "deleted": primaries["docs"]["deleted"],
            "size_bytes": primaries["store"]["size_in_bytes"],
        }
    except Exception as exc:  # noqa: BLE001
        return {"index": index, "error": str(exc)}
