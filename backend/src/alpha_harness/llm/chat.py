"""A conversation with the assistant, and the field picks that come out of it.

Anything a consultant types steers the search, so the assistant's job here is narrow: read
the hunch, and come back with **actual data fields** to run on. The conversation is the
input; a field list is the output; the labs take it from there.

Two controls:

* **Model.** One of those the user set up, each with its own daily budget; running out
  is the thing that ends a session. See :mod:`.registry`.
* **Reasoning.** Thinking tokens are billed like the answer's, so "think harder" is a real
  cost rather than a free upgrade.

History lives in the database so a conversation survives a restart.
"""

from typing import TYPE_CHECKING, Any, Literal

import structlog
from sqlalchemy import select

from ..db.models import ChatMessage, ChatThread, utcnow
from .prompts import ASSISTANT
from .text import clip, loads_or

if TYPE_CHECKING:
    from ..catalog.queries import CatalogQueries, Tuple4
    from ..db.sqlite import Database
    from .service import LLMService

log = structlog.get_logger(__name__)

#: How much of a conversation is replayed to the model. Long enough to hold a thread,
#: short enough that prompt tokens do not eat the per-minute budget.
HISTORY_TURNS = 12

type Reasoning = Literal["quick", "normal", "careful", "deep"]

#: Reasoning effort, in words rather than token counts. The mapping is to Google's own
#: thinking levels; the labels are what a consultant sees.
REASONING: dict[Reasoning, dict[str, Any]] = {
    "quick": {
        "level": "MINIMAL",
        "label": "Quick",
        "description": "Answers straight away. Best for looking something up.",
    },
    "normal": {
        "level": "LOW",
        "label": "Normal",
        "description": "Thinks a little first. The right choice almost always.",
    },
    "careful": {
        "level": "MEDIUM",
        "label": "Careful",
        "description": "Works through it properly. Slower, and uses more of the daily budget.",
    },
    "deep": {
        "level": "HIGH",
        "label": "Deep",
        "description": (
            "Thinks hard before answering. Save it for a genuinely difficult question — "
            "it is the most expensive setting."
        ),
    },
}

DEFAULT_REASONING: Reasoning = "normal"


def reasoning_options() -> list[dict[str, Any]]:
    return [{"value": key, **value} for key, value in REASONING.items()]


#: What the assistant returns when asked to pick fields. Kept small on purpose: a
#: consultant needs a handful of things to run, not a survey.
FIELD_PICK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "reply": {"type": "string"},
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["field", "why"],
            },
        },
        "datasets": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["reply"],
}


class ChatService:
    """Conversations, and the field picks they produce."""

    def __init__(self, db: Database, llm: LLMService, queries: CatalogQueries) -> None:
        self.db = db
        self.llm = llm
        self.queries = queries

    # -- threads ---------------------------------------------------------

    async def threads(self, limit: int = 30) -> list[ChatThread]:
        async with self.db.session() as session:
            return list(
                (
                    await session.scalars(
                        select(ChatThread).order_by(ChatThread.updated_at.desc()).limit(limit)
                    )
                ).all()
            )

    async def thread(self, thread_id: int) -> tuple[ChatThread, list[ChatMessage]] | None:
        async with self.db.session() as session:
            thread = await session.get(ChatThread, thread_id)
            if thread is None:
                return None
            messages = list(
                (
                    await session.scalars(
                        select(ChatMessage)
                        .where(ChatMessage.thread_id == thread_id)
                        .order_by(ChatMessage.id)
                    )
                ).all()
            )
            return thread, messages

    async def start(self, title: str, scope: Tuple4) -> ChatThread:
        async with self.db.session() as session:
            thread = ChatThread(
                title=title[:120],
                instrument_type=scope.instrument_type,
                region=scope.region,
                delay=scope.delay,
                universe=scope.universe,
            )
            session.add(thread)
            await session.commit()
            await session.refresh(thread)
        return thread

    async def delete(self, thread_id: int) -> None:
        async with self.db.session() as session:
            thread = await session.get(ChatThread, thread_id)
            if thread is not None:
                await session.delete(thread)
                await session.commit()

    # -- the turn --------------------------------------------------------

    async def say(
        self,
        thread_id: int | None,
        text: str,
        *,
        scope: Tuple4,
        model: str | None,
        reasoning: Reasoning,
        dataset_ids: list[str],
        system: str = ASSISTANT,
    ) -> dict[str, Any]:
        """One exchange: their words in, a reply and a set of field picks out.

        Nothing is stored until the model has answered, so a failed exchange leaves no
        conversation behind that holds only the question.
        """
        history: list[ChatMessage] = []
        if thread_id is not None:
            found = await self.thread(thread_id)
            if found is None:
                raise ValueError(f"No conversation {thread_id}.")
            _, history = found

        fields, available, catalog_note = await self._field_menu(scope, dataset_ids)
        if not fields:
            raise ValueError(
                f"No data has been downloaded for {scope.label} yet, so there is nothing "
                "for the assistant to choose from. Download this market first."
            )

        prompt = "\n\n---\n\n".join(
            [
                f"DATA FIELDS AVAILABLE IN {scope.label}:\n{fields}",
                _transcript(history),
                f"THEIR MESSAGE:\n{text}",
            ]
        )

        answer = await self.llm.generate(
            system=system,
            user=prompt,
            model_ref=model,
            temperature=0.6,
            response_schema=FIELD_PICK_SCHEMA,
            thinking=REASONING[reasoning]["level"],
        )

        parsed = _parse(answer.text)
        # Drop anything not really in the catalog: a hallucinated field costs a simulation
        # to discover.
        kept = [p for p in parsed["picks"] if p.get("field") in available]
        dropped = [p["field"] for p in parsed["picks"] if p.get("field") not in available]
        if dropped:
            log.warning("chat.invented_fields", fields=dropped[:10], scope=scope.label)

        if thread_id is None:
            thread_id = (await self.start(text, scope)).id
        await self._append(thread_id, "user", text)
        await self._append(
            thread_id,
            "assistant",
            parsed["reply"],
            picks=kept,
            # Stored, not just returned, so reopening the conversation still shows which
            # fields the model invented.
            datasets=parsed["datasets"],
            dropped=dropped,
            catalogNote=catalog_note,
            model=answer.model,
            reasoning=reasoning,
            tokens=answer.total_tokens,
        )
        await self._touch(thread_id)

        return {
            "threadId": thread_id,
            "reply": parsed["reply"],
            "picks": kept,
            "datasets": parsed["datasets"],
            "dropped": dropped,
            "catalogNote": catalog_note,
            "usage": answer.usage,
            "model": answer.model,
            "reasoning": reasoning,
        }

    # -- helpers ---------------------------------------------------------

    async def _field_menu(
        self, scope: Tuple4, dataset_ids: list[str] | None
    ) -> tuple[str, set[str], str | None]:
        """The fields the assistant may choose from: the text, their ids, and a note.

        Capped, and ordered by coverage: a field present on 4% of the market cannot carry a
        signal about it however good the idea, and listing thousands would spend the whole
        context window on names. The coverage floor is lifted once a dataset is named,
        because event-driven data is thin by nature and whole datasets sit below it.
        """
        from ..catalog.queries import FieldFilter

        page = await self.queries.fields(
            scope,
            FieldFilter(
                dataset_ids=list(dataset_ids or []),
                coverage_min=None if dataset_ids else 0.5,
                sort_by="coverage",
                sort_desc=True,
                limit=400,
            ),
        )
        rows = page["results"]
        if not rows:
            return "", set(), None

        lines = [
            f"  {r['field_id']} ({r.get('dataset_id')}): {clip(r.get('description'), 110)}"
            for r in rows
        ]
        note = None
        if int(page["total"]) > len(rows):
            note = (
                f"The assistant was shown the {len(rows)} widest-coverage fields of "
                f"{page['total']} in this market."
            )
        return "\n".join(lines), {str(r["field_id"]) for r in rows}, note

    async def _append(self, thread_id: int, role: str, text: str, **meta: Any) -> None:
        async with self.db.session() as session:
            session.add(
                ChatMessage(
                    thread_id=thread_id,
                    role=role,
                    text=text,
                    meta={k: v for k, v in meta.items() if v is not None},
                )
            )
            await session.commit()

    async def _touch(self, thread_id: int) -> None:
        async with self.db.session() as session:
            thread = await session.get(ChatThread, thread_id)
            if thread is not None:
                thread.updated_at = utcnow()
                await session.commit()


def _transcript(history: list[ChatMessage]) -> str:
    recent = history[-HISTORY_TURNS:]
    if not recent:
        return "THE CONVERSATION SO FAR:\n  (this is the first message)"
    lines: list[str] = []
    for m in recent:
        if m.role == "user":
            lines.append(f"  They: {m.text.strip()[:600]}")
            continue
        # Replay its own picks, so "why the second one?" can be answered: the reply names
        # them in prose the model no longer sees.
        picks = ", ".join(str(p.get("field")) for p in (m.meta or {}).get("picks") or [])
        said = f"  You: {m.text.strip()[:600]}"
        lines.append(f"{said} [picked: {picks}]" if picks else said)
    return "THE CONVERSATION SO FAR:\n" + "\n".join(lines)


def _parse(text: str) -> dict[str, Any]:
    """Read the model's JSON, and degrade to plain text rather than failing.

    A reply the consultant can read is worth more than a parse error, so an unparseable
    response becomes the reply itself with no picks.
    """
    payload = loads_or(text)
    if not payload:
        return {"reply": text.strip(), "picks": [], "datasets": []}

    picks = payload.get("picks")
    datasets = payload.get("datasets")
    return {
        "reply": str(payload.get("reply") or "").strip() or text.strip(),
        "picks": [p for p in picks if isinstance(p, dict) and p.get("field")]
        if isinstance(picks, list)
        else [],
        "datasets": [str(d) for d in datasets if d] if isinstance(datasets, list) else [],
    }
