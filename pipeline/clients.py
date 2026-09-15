"""Lazily-constructed external clients (Mongo, Voyage, S3, SQS).

Clients are built on first use and cached per-process, so importing an activity
module never opens a socket. Activities run in the worker process (outside the
Temporal workflow sandbox), so real I/O clients are safe here.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING

from .config import settings


def keycard_enabled() -> bool:
    return bool(settings.keycard_zone_url)


def _granted(resource: str) -> "str | None":
    """The credential the KeycardInterceptor minted for this activity execution,
    selected by resource; None outside a granted activity."""
    try:
        from keycardai.temporal import access

        return access(resource).access_token
    except Exception:
        return None

if TYPE_CHECKING:  # avoid importing heavy deps at module load
    import boto3
    import voyageai
    from pymongo import MongoClient


@lru_cache(maxsize=1)
def _mongo_client_for(uri: str) -> "MongoClient":
    from pymongo import MongoClient

    return MongoClient(uri, appname="teamporal-app")


def mongo_client() -> "MongoClient":
    if keycard_enabled():
        uri = _granted(settings.keycard_mongodb_resource) or settings.mongodb_uri
        if not uri:
            raise RuntimeError(
                "Keycard mode: MongoDB access outside a granted activity. Run "
                "through a workflow (make index does), or set MONGODB_URI in "
                ".env as a fallback for out-of-band scripts."
            )
    else:
        uri = settings.mongodb_uri
    if not uri:
        raise RuntimeError(
            "MONGODB_URI is not set — populate .env before running, "
            "or configure Keycard (KEYCARD_ZONE_URL) to vault it."
        )
    # Cached per URI: an unchanged credential reuses one connection pool, and a
    # rotation in Keycard yields a new URI, hence a fresh client, on the next
    # activity execution.
    return _mongo_client_for(uri)


def knowledge_collection(name: str | None = None):
    db = mongo_client()[settings.mongodb_db]
    return db[name or settings.knowledge_collection]


@lru_cache(maxsize=1)
def _voyage_client_for(api_key: str) -> "voyageai.Client":
    import voyageai

    return voyageai.Client(api_key=api_key)


def voyage_client() -> "voyageai.Client":
    if keycard_enabled():
        key = _granted(settings.keycard_voyage_resource) or settings.voyage_api_key
        if not key:
            raise RuntimeError(
                "Keycard mode: Voyage access outside a granted activity; the "
                "embed, rerank, and search activities declare it in @grant."
            )
    else:
        key = settings.voyage_api_key
    if not key:
        raise RuntimeError(
            "VOYAGE_API_KEY is not set — populate .env before running, "
            "or configure Keycard (KEYCARD_ZONE_URL) to vault it."
        )
    return _voyage_client_for(key)


def _aws_kwargs() -> dict:
    """Common boto3 kwargs: region + explicit creds when provided in settings.

    boto3 does not read our .env, so any creds set there (incl. MinIO's
    minioadmin/minioadmin) must be passed explicitly. When blank, boto3 falls back
    to its standard credential chain (profile / role / actual env vars).
    """
    kwargs: dict = {"region_name": settings.aws_region}
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return kwargs


@lru_cache(maxsize=1)
def s3_client():
    import boto3
    from botocore.config import Config

    kwargs = _aws_kwargs()
    if settings.s3_endpoint_url:
        # MinIO (or any S3-compatible endpoint) needs an explicit endpoint and
        # path-style addressing (no virtual-host buckets).
        kwargs["endpoint_url"] = settings.s3_endpoint_url
        kwargs["config"] = Config(s3={"addressing_style": "path"})
    return boto3.client("s3", **kwargs)


@lru_cache(maxsize=1)
def sqs_client():
    import boto3

    return boto3.client("sqs", **_aws_kwargs())
