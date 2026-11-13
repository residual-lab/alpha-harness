"""One client for every provider: each of them, Google included, speaks chat-completions.

Built on the OpenAI SDK, which also streams. Google's endpoint is its OpenAI-compatible
one, which takes a reasoning effort.
"""

import asyncio
from typing import TYPE_CHECKING, Any

import structlog
from openai import APIStatusError, AsyncOpenAI, omit

if TYPE_CHECKING:
    import httpx2
    from openai.types.chat import ChatCompletionMessageParam
    from openai.types.chat.completion_create_params import ResponseFormat
    from openai.types.shared import ReasoningEffort

log = structlog.get_logger(__name__)

#: Waits before sending again after a 503: an overloaded model usually clears in seconds.
OVERLOADED_WAITS = (2.0, 5.0)

#: How much of a provider's error is kept. All of it that matters: whether a Google 429 is
#: the per-minute or the per-day limit is said only in its ``details``, near the end.
DETAIL_CHARS = 2_000


class ProviderError(RuntimeError):
    """A provider's refusal: its HTTP status, and its own words about why."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status} {detail}")
        self.status = status


class OpenAICompatible:
    """A chat-completions client for one key, on a connection pool it shares."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        http: httpx2.AsyncClient,
        headers: dict[str, str] | None = None,
    ) -> None:
        # No SDK retries: a 429 means this key is spent for now, which rotation answers by
        # moving to the next key, and sending it again only burns the day's requests.
        self._sdk = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            http_client=http,
            max_retries=0,
            default_headers=headers,
        )

    async def generate(
        self,
        *,
        model: str,
        system: str,
        user: str,
        temperature: float | None,
        response_format: ResponseFormat | None,
        reasoning_effort: ReasoningEffort = None,
        prompt_cache_key: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        """One completion: its text, and the ``usage`` the provider reported.

        ``response_format`` and ``prompt_cache_key`` go as given, or not at all when ``None``.
        A 503 is sent again after a short wait.
        """
        args = (
            model,
            system,
            user,
            temperature,
            response_format,
            reasoning_effort,
            prompt_cache_key,
        )
        for wait in OVERLOADED_WAITS:
            try:
                return await self._complete(*args)
            except ProviderError as exc:
                if exc.status != 503:
                    raise
            log.info("llm.overloaded", model=model, wait=wait)
            await asyncio.sleep(wait)
        return await self._complete(*args)

    async def _complete(
        self,
        model: str,
        system: str,
        user: str,
        temperature: float | None,
        response_format: ResponseFormat | None,
        reasoning_effort: ReasoningEffort,
        prompt_cache_key: str | None,
    ) -> tuple[str, dict[str, Any]]:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        try:
            completion = await self._sdk.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature if temperature is not None else omit,
                response_format=response_format if response_format is not None else omit,
                reasoning_effort=reasoning_effort if reasoning_effort is not None else omit,
                prompt_cache_key=prompt_cache_key if prompt_cache_key is not None else omit,
            )
        except APIStatusError as exc:
            raise ProviderError(exc.status_code, _detail(exc.response)) from exc
        text = completion.choices[0].message.content if completion.choices else None
        return text or "", completion.usage.model_dump() if completion.usage else {}

    async def models(self, **query: object) -> list[str]:
        """Model ids this key can reach. Not billed against the generation quota."""
        try:
            page = await self._sdk.models.list(extra_query=query or None)
        except APIStatusError as exc:
            raise ProviderError(exc.status_code, _detail(exc.response)) from exc
        # Google's ids come back as ``models/gemini-…``; its chat endpoint takes either.
        return sorted({model.id.removeprefix("models/") for model in page.data})


def _detail(response: httpx2.Response) -> str:
    """The provider's own words about what went wrong, details included."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:DETAIL_CHARS]
    # Google's compatible endpoint answers with a list holding the one error.
    if isinstance(body, list) and body and isinstance(body[0], dict):
        body = body[0]
    error = body.get("error") if isinstance(body, dict) else None
    return str(error or body)[:DETAIL_CHARS]
