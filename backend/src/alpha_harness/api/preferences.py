"""The Settings screen's choices, and the recipes each lab's screen keeps."""

from fastapi import APIRouter

from .. import preferences
from ..preferences import BreakerReshapes, Preferences, SuperLabRecipes, TagGroups
from .deps import State

router = APIRouter(prefix="/api/preferences", tags=["preferences"])


@router.get("")
async def read(state: State) -> Preferences:
    return await preferences.load(state.db)


@router.put("")
async def write(body: Preferences, state: State) -> Preferences:
    """Replace every choice. Each takes effect at once, not after a restart."""
    await preferences.save(state.db, body)
    preferences.apply(state, body)
    return body


@router.get("/super-lab")
async def read_super_lab(state: State) -> SuperLabRecipes | None:
    """Null until the screen first saves, which is when it uploads the copy it kept itself."""
    return await preferences.kept(state.db, preferences.SUPER_LAB, SuperLabRecipes)


@router.put("/super-lab")
async def write_super_lab(body: SuperLabRecipes, state: State) -> SuperLabRecipes:
    await preferences.keep(state.db, preferences.SUPER_LAB, body)
    return body


@router.get("/correlation-breaker")
async def read_breaker(state: State) -> BreakerReshapes | None:
    """Null until the screen first saves, which is when it uploads the copy it kept itself."""
    return await preferences.kept(state.db, preferences.BREAKER, BreakerReshapes)


@router.put("/correlation-breaker")
async def write_breaker(body: BreakerReshapes, state: State) -> BreakerReshapes:
    await preferences.keep(state.db, preferences.BREAKER, body)
    return body


@router.get("/tag-groups")
async def read_tag_groups(state: State) -> TagGroups | None:
    """Null until the screen first saves, which is when it uploads the copy it kept itself."""
    return await preferences.kept(state.db, preferences.TAG_GROUPS, TagGroups)


@router.put("/tag-groups")
async def write_tag_groups(body: TagGroups, state: State) -> TagGroups:
    await preferences.keep(state.db, preferences.TAG_GROUPS, body)
    return body
