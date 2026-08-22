"""Weighted similarity between games.

Deliberately plain arithmetic: no embeddings and no similarity queries in the database.
Every game is reduced to a few comparable feature sets, each set is compared with the same
overlap measure, and the components are combined with fixed weights.

Two rules shape the result:

* A component only counts when **both** games have the data for it. The weights of the
  components that could be compared form the denominator, so a game is judged on what is
  known about it rather than marked down feature by feature for what is missing.
* The result is then scaled by ``sqrt(share of weight that could be compared)``. Without
  it a barely-known game that merely shares a genre outscores a genuinely similar game
  with full data, because a single matching component averages to a perfect score. The
  factor expresses confidence in the comparison, not a judgement of the game.
* Overlap is ``|A∩B| / sqrt(|A|·|B|)``. Plain Jaccard punishes a richly tagged game for
  having extra tags; this geometric form stays fair when one game carries more tags.
"""

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.models import Game

# Weights are relative importance, not percentages: the denominator is whatever could be
# compared. They were tuned by eye against the collected catalogue (see AGENTS.md).
TAG_FACET_WEIGHTS: dict[str, float] = {
    "mechanics": 0.28,  # what you actually do; the strongest signal for "plays like"
    "setting": 0.12,  # world and theme
    "structure": 0.10,  # solo/co-op, story-driven, run-based, length
    "style": 0.10,  # perspective and art direction
    "descriptors": 0.06,  # free-form specifics such as "rail-shooter"
    "mood": 0.05,  # tone; weaker because most games claim several
}
WEIGHTS: dict[str, float] = {
    **TAG_FACET_WEIGHTS,
    "genres": 0.16,  # Metacritic's own classification, factual rather than inferred
    "related": 0.08,  # Metacritic lists the other game among this one's genre peers
    "developer": 0.06,  # same studio usually means a familiar feel
    "score": 0.04,  # comparable quality band
    "esrb": 0.03,  # who the game is made for
    "publisher": 0.02,
    "platforms": 0.02,  # nearly everything ships on PC, so this says little
    "release_year": 0.02,
}
TOTAL_WEIGHT = sum(WEIGHTS.values())

SCORE_SPAN = 30.0  # metascore points at which the quality bands stop being comparable
YEAR_SPAN = 8.0
MIN_SCORE = 0.12  # below this the "match" is noise, so nothing is shown

FACET_LABELS = {
    "mechanics": "механика",
    "setting": "сеттинг",
    "style": "подача",
    "structure": "структура",
    "mood": "настроение",
    "descriptors": "особенности",
}


@dataclass(slots=True)
class GameFeatures:
    game_id: Any
    title: str
    facets: dict[str, set[str]] = field(default_factory=dict)
    genres: set[str] = field(default_factory=set)
    platforms: set[str] = field(default_factory=set)
    developer: str | None = None
    developer_name: str | None = None
    publisher: str | None = None
    release_year: int | None = None
    metascore: int | None = None
    esrb_rating: str | None = None
    slug: str | None = None
    related_slugs: set[str] = field(default_factory=set)

    @property
    def has_tags(self) -> bool:
        return any(self.facets.values())


@dataclass(slots=True)
class Component:
    name: str
    weight: float
    score: float
    shared: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Comparison:
    """One pairwise result, keeping the two ideas separate and inspectable.

    ``raw`` answers "how alike are the features we could compare"; ``confidence`` answers
    "how much of the feature set could be compared at all"; ``score`` combines them.
    """

    raw: float
    confidence: float
    components: list["Component"]

    @property
    def score(self) -> float:
        return self.raw * self.confidence


@dataclass(slots=True)
class SimilarGame:
    game: Game
    score: float
    components: list[Component]
    reasons: list[str]


def lead_platform(game: Game) -> Any:
    """The platform a game is represented by: most critic reviews, then highest Metascore.

    The catalogue shows one score per game, and it must be the same one everywhere, so this
    choice is shared by the list, the sorting and the similarity comparison.
    """
    rows = [row for row in game.platforms]
    if not rows:
        return None
    return max(
        rows,
        key=lambda row: (
            row.critic_review_count or 0,
            row.metascore or -1,
            row.user_rating_count or 0,
        ),
    )


def features_of(game: Game) -> GameFeatures:
    facets: dict[str, set[str]] = {}
    for tag in game.tags:
        facets.setdefault(tag.facet or "descriptors", set()).add(tag.slug)
    lead = lead_platform(game)
    return GameFeatures(
        game_id=game.id,
        title=game.title,
        facets=facets,
        genres={genre.slug for genre in game.genres},
        platforms={row.platform.slug for row in game.platforms if row.platform is not None},
        developer=(game.developer or "").strip().casefold() or None,
        developer_name=(game.developer or "").strip() or None,
        publisher=(game.publisher or "").strip().casefold() or None,
        release_year=game.release_date.year if isinstance(game.release_date, date) else None,
        metascore=lead.metascore if lead is not None else None,
        esrb_rating=(getattr(game, "esrb_rating", None) or "").strip().upper() or None,
        slug=getattr(game, "slug", None),
        related_slugs=set(getattr(game, "related_slugs", None) or ()),
    )


def set_overlap(left: set[str], right: set[str]) -> float | None:
    """Geometric overlap, or None when either side has nothing to compare."""
    if not left or not right:
        return None
    return len(left & right) / math.sqrt(len(left) * len(right))


def _closeness(left: float | None, right: float | None, span: float) -> float | None:
    if left is None or right is None:
        return None
    return max(0.0, 1.0 - abs(left - right) / span)


def compare(left: GameFeatures, right: GameFeatures) -> Comparison:
    components: list[Component] = []

    # The keys are the complete AI tag schema, including free descriptors. Keeping the
    # iteration tied to the component weights prevents a displayed facet from silently
    # becoming decoration-only when the vocabulary changes (for example, `anime` is a
    # setting value and therefore contributes through the setting component).
    for facet, weight in TAG_FACET_WEIGHTS.items():
        first, second = left.facets.get(facet, set()), right.facets.get(facet, set())
        score = set_overlap(first, second)
        if score is not None:
            components.append(Component(facet, weight, score, sorted(first & second)))

    genre_score = set_overlap(left.genres, right.genres)
    if genre_score is not None:
        components.append(
            Component("genres", WEIGHTS["genres"], genre_score, sorted(left.genres & right.genres))
        )

    platform_score = set_overlap(left.platforms, right.platforms)
    if platform_score is not None:
        components.append(Component("platforms", WEIGHTS["platforms"], platform_score))

    if left.developer and right.developer:
        same = left.developer == right.developer
        components.append(Component("developer", WEIGHTS["developer"], 1.0 if same else 0.0))
    if left.publisher and right.publisher:
        same = left.publisher == right.publisher
        components.append(Component("publisher", WEIGHTS["publisher"], 1.0 if same else 0.0))

    year_score = _closeness(left.release_year, right.release_year, YEAR_SPAN)
    if year_score is not None:
        components.append(Component("release_year", WEIGHTS["release_year"], year_score))

    score_score = _closeness(left.metascore, right.metascore, SCORE_SPAN)
    if score_score is not None:
        components.append(Component("score", WEIGHTS["score"], score_score))

    if left.esrb_rating and right.esrb_rating:
        same = left.esrb_rating == right.esrb_rating
        components.append(Component("esrb", WEIGHTS["esrb"], 1.0 if same else 0.0))

    if (left.related_slugs or right.related_slugs) and left.slug and right.slug:
        linked = right.slug in left.related_slugs or left.slug in right.related_slugs
        components.append(Component("related", WEIGHTS["related"], 1.0 if linked else 0.0))

    available = sum(component.weight for component in components)
    if available <= 0:
        return Comparison(raw=0.0, confidence=0.0, components=components)
    raw = sum(component.weight * component.score for component in components) / available
    # A match built from one comparable component is not as trustworthy as one built from
    # the whole feature set, even when that single component matches perfectly.
    confidence = math.sqrt(min(available / TOTAL_WEIGHT, 1.0))
    return Comparison(raw=raw, confidence=confidence, components=components)


def _reasons(left: GameFeatures, right: GameFeatures, components: list[Component]) -> list[str]:
    reasons: list[str] = []
    ranked = sorted(components, key=lambda c: c.weight * c.score, reverse=True)
    for component in ranked:
        if component.score <= 0:
            continue
        if component.name == "genres" and component.shared:
            reasons.append("Общий жанр: " + ", ".join(component.shared).replace("-", " "))
        elif component.name in FACET_LABELS and component.shared:
            label = FACET_LABELS[component.name]
            shared = ", ".join(tag.replace("-", " ") for tag in component.shared[:3])
            reasons.append(f"Общая {label}: {shared}")
        elif component.name == "developer":
            reasons.append(f"Один разработчик: {right.developer_name or left.developer_name}")
        elif component.name == "related":
            reasons.append("Metacritic относит игры к похожим")
        elif component.name == "score" and component.score > 0.8:
            reasons.append("Близкий Metascore")
        if len(reasons) >= 3:
            break
    return reasons


def rank_similar(target: Game, candidates: list[Game], limit: int = 6) -> list[SimilarGame]:
    """Best matches for one game, most similar first."""
    left = features_of(target)
    scored: list[SimilarGame] = []
    for candidate in candidates:
        if candidate.id == target.id:
            continue
        right = features_of(candidate)
        comparison = compare(left, right)
        if comparison.score < MIN_SCORE:
            continue
        scored.append(
            SimilarGame(
                game=candidate,
                score=comparison.score,
                components=comparison.components,
                reasons=_reasons(left, right, comparison.components),
            )
        )
    scored.sort(key=lambda item: (item.score, item.game.title), reverse=True)
    return scored[:limit]
