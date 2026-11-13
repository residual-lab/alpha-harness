"""A Key and model stored for a retired provider stay visible, never used, never Google's."""

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import httpx2
import pytest
from openai import DefaultAsyncHttpxClient

from alpha_harness.api import llm as api
from alpha_harness.db.models import ApiKey, KeyUsage, LLMModel
from alpha_harness.db.sqlite import Database
from alpha_harness.llm import providers
from alpha_harness.llm.keys import KEY_CONTEXT, LLMError, fingerprint, hint
from alpha_harness.llm.registry import ModelRegistry
from alpha_harness.llm.service import LLMService
from alpha_harness.sealing import Sealer

if TYPE_CHECKING:
    from pathlib import Path

SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def test_lookup_never_falls_back_to_google() -> None:
    assert providers.get(None) is providers.PROVIDERS["google"]
    assert providers.get("") is providers.PROVIDERS["google"]
    assert providers.get("github").label == "GitHub Models (retired)"
    assert providers.get("github").retired
    assert providers.get("never-heard-of-it").retired
    assert providers.get("github").base_url == ""
    assert "github" not in providers.PROVIDERS
    assert [p.id for p in providers.catalogue().retired] == ["github"]


def test_stored_github_rows_are_labelled_refused_and_never_sent(tmp_path: Path) -> None:
    sent: list[httpx2.Request] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        sent.append(request)
        return httpx2.Response(500)

    async def run() -> None:
        db = Database(tmp_path / "harness.db")
        await db.create_all()
        sealer = Sealer(tmp_path / "key")
        async with db.session() as session:
            for provider, secret in (("github", "ghp_throwaway0000"), ("google", "AIzaThrowaway")):
                session.add(
                    ApiKey(
                        label=provider,
                        provider=provider,
                        key_sealed=sealer.seal(secret, context=KEY_CONTEXT),
                        hint=hint(secret),
                        fingerprint=fingerprint(secret),
                    )
                )
                session.add(
                    LLMModel(
                        provider=provider,
                        model=f"{provider}-model",
                        # The retired one is the richest, so a default pick would choose it.
                        requests_per_minute=60,
                        requests_per_day=9000 if provider == "github" else 100,
                        reset_timezone="UTC",
                    )
                )
            await session.flush()
            session.add(KeyUsage(api_key_id=1, model="github-model", day="2000-01-01", requests=3))
        registry = ModelRegistry(db)
        await registry.load()
        llm: Any = LLMService(db, sealer, registry)
        await llm._http.aclose()
        llm._http = DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handle))
        state: Any = SimpleNamespace(llm=llm)

        status = await api.list_keys(state)
        by_provider = {k.provider: k for k in status.keys}
        assert by_provider["github"].retired
        assert by_provider["google"].retired is None
        assert status.enabled == 1
        assert [b.provider for b in status.budget] == ["google"]

        roster = {m.provider: m for m in (await api.models(state)).models}
        assert roster["github"].retired and roster["google"].retired is None
        assert [m["provider"] for m in await llm.runnable()] == ["google"]

        with pytest.raises(LLMError, match="stopped serving"):
            await llm.generate(
                system="s",
                user="u",
                model_ref="github:github-model",
                response_schema=SCHEMA,
                temperature=0.5,
            )
        # Naming no model must skip it too, though it has the most requests a day.
        assert llm.model_for(None).provider == "google"

        assert (await llm.check_key(1))["ok"] is False
        assert "stopped serving" in (await llm.check_key(1))["error"]
        assert (await llm.offered("github"))["models"] == []
        with pytest.raises(LLMError):
            await llm.keys.add("ghp_another", provider="github")
        with pytest.raises(LLMError):
            await registry.set("github", "x", 1, 1, "UTC")

        await registry.remove("github", "github-model")
        await llm.keys.remove(1)
        assert [k.provider for k in (await api.list_keys(state)).keys] == ["google"]
        await llm.aclose()
        await db.dispose()

    asyncio.run(run())
    assert not sent, [str(r.url) for r in sent]
