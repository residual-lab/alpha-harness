"""Which assistants can answer, and how to get a key for each.

Every provider here has a **free tier that needs no card**. That is the whole selection
rule: the assistant is optional, so a provider that asks for payment details first turns an
optional convenience into a purchase decision.

All of them speak the OpenAI chat-completions protocol, Google through its compatible
endpoint, which is why one small client in :mod:`.openai_compat` serves every one.

**A key only ever answers for its own provider**, so rotation filters on provider before it
looks at budget.

**No models or limits live here.** Both change every few weeks, so the user sets each model
up with the limits their provider shows them; ``limits_url`` is where to look.
"""

from dataclasses import dataclass

from ..schemas import Out


@dataclass(frozen=True, slots=True)
class Provider:
    """One assistant, and what it takes to start using it."""

    id: str
    label: str
    #: Where its chat-completions endpoint lives.
    base_url: str
    #: Where to get a key. Shown as a link, because "search for it" loses people.
    onboarding_url: str
    #: Where the provider shows its rate limits, the account's own page where there is one.
    limits_url: str
    #: What a key from this provider looks like, so a pasted wrong one is caught early.
    key_hint: str
    #: True for a provider that bills the user. Kept apart everywhere it is shown, and its
    #: keys are refused without a daily cap — see :meth:`KeyStore.add`.
    paid: bool = False
    #: What a paid tier bills, in plain words; shown above its key field.
    tier_note: str = ""
    #: Why this provider can no longer answer; empty for one that can.
    retired: str = ""


class LLMProvider(Out):
    """:class:`Provider` on the wire."""

    id: str
    label: str
    onboarding_url: str
    limits_url: str
    key_hint: str
    tier_note: str
    #: True for a provider that bills the user rather than offering a free tier.
    paid: bool


class RetiredProvider(Out):
    """A provider that is gone but still has Keys or models stored under it."""

    id: str
    label: str
    note: str


class LLMProviders(Out):
    providers: list[LLMProvider]
    #: Gone providers, so a Key or model left behind can be named and its removal explained.
    retired: list[RetiredProvider]
    default: str
    paid_note: str


PROVIDERS: dict[str, Provider] = {
    "google": Provider(
        id="google",
        label="Google AI Studio",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        onboarding_url="https://aistudio.google.com/apikey",
        limits_url="https://aistudio.google.com/rate-limit",
        key_hint="AIza…",
    ),
    "groq": Provider(
        id="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        onboarding_url="https://console.groq.com/keys",
        limits_url="https://console.groq.com/settings/limits",
        key_hint="gsk_…",
    ),
    "cerebras": Provider(
        id="cerebras",
        label="Cerebras",
        base_url="https://api.cerebras.ai/v1",
        onboarding_url="https://cloud.cerebras.ai/",
        limits_url="https://inference-docs.cerebras.ai/support/rate-limits",
        key_hint="csk-…",
    ),
    "openrouter": Provider(
        id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        onboarding_url="https://openrouter.ai/keys",
        limits_url="https://openrouter.ai/docs/api-reference/limits",
        key_hint="sk-or-…",
    ),
    "nvidia": Provider(
        id="nvidia",
        label="NVIDIA NIM",
        base_url="https://integrate.api.nvidia.com/v1",
        onboarding_url="https://build.nvidia.com/",
        limits_url="https://build.nvidia.com/faq",
        key_hint="nvapi-…",
    ),
    "mistral": Provider(
        id="mistral",
        label="Mistral",
        base_url="https://api.mistral.ai/v1",
        onboarding_url="https://console.mistral.ai/api-keys/",
        limits_url="https://admin.mistral.ai/plateforme/limits",
        key_hint="…",
    ),
    "huggingface": Provider(
        id="huggingface",
        label="Hugging Face",
        base_url="https://router.huggingface.co/v1",
        onboarding_url="https://huggingface.co/settings/tokens",
        limits_url="https://huggingface.co/docs/inference-providers/pricing",
        key_hint="hf_…",
    ),
    # --- bring your own, and your own bill ---------------------------------------------
    #
    # Everything above is free because the assistant is optional and a card turns an
    # optional convenience into a purchase decision. These two are for the user who already
    # pays for one of them and would rather spend that than a free tier's daily limit.
    # They are kept behind their own heading in the UI so the default stays
    # "no card", and a key for either is refused without a daily request cap: the free
    # providers stop on their own, and these stop only when told to.
    "openai": Provider(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        onboarding_url="https://platform.openai.com/api-keys",
        limits_url="https://platform.openai.com/settings/organization/limits",
        key_hint="sk-…",
        tier_note="Your own OpenAI account — billed to you, not free.",
        paid=True,
    ),
    "anthropic": Provider(
        id="anthropic",
        # Anthropic's OpenAI-compatible endpoint, which takes the same Bearer token and the
        # same ``/chat/completions`` shape, so no second client is needed. Anthropic calls
        # it a compatibility layer for evaluation rather than a production path; the ways it
        # differs that matter here are that ``response_format`` must be a strict JSON schema
        # (a bare JSON object is a 400), that it caches no prompts, and that thinking output
        # is not returned, which nothing here reads.
        label="Anthropic Claude",
        base_url="https://api.anthropic.com/v1",
        onboarding_url="https://platform.claude.com/settings/keys",
        limits_url="https://platform.claude.com/settings/limits",
        key_hint="sk-ant-…",
        tier_note="Your own Anthropic account — billed to you, not free.",
        paid=True,
    ),
}

#: Providers that shut down. Rows stored for one are kept, labelled and never used, because
#: deleting a user's Key unasked is worse than showing it once more.
RETIRED: dict[str, Provider] = {
    "github": Provider(
        id="github",
        label="GitHub Models (retired)",
        base_url="",
        onboarding_url="",
        limits_url="",
        key_hint="",
        retired="GitHub Models stopped serving requests on 30 July 2026.",
    ),
}

DEFAULT_PROVIDER = "google"

#: Said once, under the paid heading. The cap is the whole point: a free tier stops by
#: itself and a paid account does not, so the app will not hold one of these keys until it
#: has been told where to stop.
PAID_NOTE = (
    "These bill your own account. Nothing here is needed — every provider above is free — "
    "but a key you already pay for is not held to a free tier's daily limit. You set a "
    "daily request cap when you add one, and Alpha Harness stops at it."
)


def get(provider_id: str | None) -> Provider:
    """One provider: Google when nothing was said, and for a stored id this build no longer
    offers, a stub whose ``retired`` says so. Never Google, which would be sent that
    provider's key."""
    if not provider_id:
        return PROVIDERS[DEFAULT_PROVIDER]
    return (
        PROVIDERS.get(provider_id)
        or RETIRED.get(provider_id)
        or Provider(
            id=provider_id,
            label=f"{provider_id} (unknown)",
            base_url="",
            onboarding_url="",
            limits_url="",
            key_hint="",
            retired="This application does not know that provider.",
        )
    )


def catalogue() -> LLMProviders:
    """Every provider, Google first and the paid ones last, for the screen where a key is
    added. The order is the argument: free is the default because it is first and because
    nothing below the fold is needed to use the app."""
    return LLMProviders(
        providers=[
            LLMProvider.model_validate(p, from_attributes=True)
            for p in sorted(PROVIDERS.values(), key=lambda p: p.paid)
        ],
        retired=[RetiredProvider(id=p.id, label=p.label, note=p.retired) for p in RETIRED.values()],
        default=DEFAULT_PROVIDER,
        paid_note=PAID_NOTE,
    )
