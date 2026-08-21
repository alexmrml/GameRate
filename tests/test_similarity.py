"""Weighted similarity: overlap maths, missing data, and the lead-platform rule."""

from datetime import date

from app.services.similarity import (
    GameFeatures,
    compare,
    lead_platform,
    rank_similar,
    set_overlap,
)


class FakeTag:
    def __init__(self, slug: str, facet: str) -> None:
        self.slug = slug
        self.facet = facet
        self.name = slug.replace("-", " ")


class FakeGenre:
    def __init__(self, slug: str) -> None:
        self.slug = slug
        self.name = slug


class FakePlatformRow:
    def __init__(self, slug, metascore=None, userscore=None, critics=0, users=0):
        self.platform = FakeGenre(slug)
        self.platform_id = slug
        self.metascore = metascore
        self.userscore = userscore
        self.critic_review_count = critics
        self.user_rating_count = users


class FakeGame:
    def __init__(
        self,
        game_id,
        title,
        *,
        tags=(),
        genres=(),
        platforms=(),
        developer=None,
        publisher=None,
        release=None,
    ):
        self.id = game_id
        self.title = title
        self.tags = [FakeTag(slug, facet) for facet, slug in tags]
        self.genres = [FakeGenre(slug) for slug in genres]
        self.platforms = list(platforms)
        self.developer = developer
        self.publisher = publisher
        self.release_date = release


def features(**kwargs) -> GameFeatures:
    kwargs.setdefault("game_id", "x")
    kwargs.setdefault("title", "X")
    return GameFeatures(**kwargs)


def test_overlap_is_fair_to_a_game_with_more_tags() -> None:
    assert set_overlap({"a", "b"}, {"a", "b"}) == 1.0
    # Two of three shared: better than Jaccard's 0.5, because the extra tag is not a fault.
    assert round(set_overlap({"a", "b"}, {"a", "b", "c"}), 3) == 0.816
    assert set_overlap({"a"}, {"b"}) == 0.0
    assert set_overlap(set(), {"a"}) is None


def test_identical_feature_sets_score_one() -> None:
    left = features(
        facets={
            "mechanics": {"platforming", "exploration"},
            "setting": {"fantasy"},
            "style": {"3d"},
            "structure": {"single-player"},
            "mood": {"cozy"},
            "descriptors": {"time-manipulation"},
        },
        genres={"platformer"},
        developer="studio",
        publisher="publisher",
        platforms={"pc"},
        release_year=2026,
        metascore=80,
        esrb_rating="E",
        slug="left",
        related_slugs={"left"},
    )
    result = compare(left, left)
    assert result.raw == 1.0
    assert result.confidence == 1.0
    assert result.score == 1.0


def test_missing_features_are_ignored_instead_of_penalised() -> None:
    rich = features(
        facets={"mechanics": {"platforming"}, "mood": {"cozy"}},
        genres={"platformer"},
        developer="studio-a",
        release_year=2026,
        metascore=80,
    )
    sparse = features(facets={"mechanics": {"platforming"}})

    result = compare(rich, sparse)

    # Only the mechanics overlap could be compared, and it is scored on its own merit.
    assert [component.name for component in result.components] == ["mechanics"]
    assert result.raw == 1.0
    # The thin comparison is still ranked below an equally strong full-feature match.
    assert result.confidence < 0.6


def test_shared_gameplay_outranks_a_shared_release_year() -> None:
    target = features(facets={"mechanics": {"roguelike", "deckbuilding"}}, release_year=2026)
    same_mechanics = features(
        facets={"mechanics": {"roguelike", "deckbuilding"}}, release_year=2010
    )
    same_year = features(facets={"mechanics": {"racing"}}, release_year=2026)

    assert compare(target, same_mechanics).score > compare(target, same_year).score


def test_score_closeness_fades_with_distance() -> None:
    near = compare(features(metascore=80), features(metascore=78))
    far = compare(features(metascore=80), features(metascore=40))
    assert near.raw > 0.9
    assert far.raw == 0.0


def test_lead_platform_is_the_one_with_the_most_critic_reviews() -> None:
    game = FakeGame(
        1,
        "Multi",
        platforms=[
            FakePlatformRow("pc", metascore=90, critics=3),
            FakePlatformRow("ps5", metascore=78, critics=17),
            FakePlatformRow("switch", metascore=None, critics=0),
        ],
    )
    lead = lead_platform(game)
    assert lead.platform.slug == "ps5"
    assert lead.metascore == 78


def test_lead_platform_falls_back_to_the_better_score_on_a_tie() -> None:
    game = FakeGame(
        1,
        "Tied",
        platforms=[
            FakePlatformRow("pc", metascore=70, critics=4),
            FakePlatformRow("ps5", metascore=85, critics=4),
        ],
    )
    assert lead_platform(game).platform.slug == "ps5"


def test_a_game_without_platforms_has_no_lead() -> None:
    assert lead_platform(FakeGame(1, "Bare")) is None


def build_catalogue() -> list[FakeGame]:
    platformer_tags = [
        ("mechanics", "platforming"),
        ("mechanics", "exploration"),
        ("setting", "fantasy"),
        ("style", "3d"),
        ("style", "third-person"),
        ("structure", "single-player"),
        ("structure", "story-driven"),
    ]
    return [
        FakeGame(
            1,
            "Clockwork Quest",
            tags=platformer_tags,
            genres=["3d-platformer"],
            platforms=[FakePlatformRow("pc", metascore=79, critics=20)],
            developer="Studio A",
            release=date(2026, 8, 13),
        ),
        FakeGame(
            2,
            "Skyward Leap",
            tags=platformer_tags[:-1] + [("mood", "whimsical")],
            genres=["3d-platformer"],
            platforms=[FakePlatformRow("pc", metascore=82, critics=15)],
            developer="Studio B",
            release=date(2026, 3, 2),
        ),
        FakeGame(
            3,
            "Gridiron 27",
            tags=[
                ("mechanics", "sports"),
                ("setting", "sports-world"),
                ("style", "realistic"),
                ("structure", "pvp-multiplayer"),
            ],
            genres=["sports"],
            platforms=[FakePlatformRow("ps5", metascore=77, critics=10)],
            developer="Studio C",
            release=date(2026, 8, 13),
        ),
        FakeGame(
            4,
            "Untagged Curio",
            genres=["adventure"],
            platforms=[FakePlatformRow("pc", metascore=60, critics=2)],
            developer="Studio D",
            release=date(2026, 1, 1),
        ),
    ]


def test_ranking_puts_the_genuinely_similar_game_first() -> None:
    catalogue = build_catalogue()
    matches = rank_similar(catalogue[0], catalogue, limit=3)

    assert matches[0].game.title == "Skyward Leap"
    assert matches[0].score > 0.6
    titles = [match.game.title for match in matches]
    assert "Gridiron 27" not in titles or titles.index("Gridiron 27") > 0


def test_matches_explain_themselves() -> None:
    catalogue = build_catalogue()
    match = rank_similar(catalogue[0], catalogue, limit=1)[0]

    assert match.reasons
    assert any("gameplay" in reason for reason in match.reasons)


def test_a_game_is_never_similar_to_itself() -> None:
    catalogue = build_catalogue()
    assert all(match.game.id != 1 for match in rank_similar(catalogue[0], catalogue))


def test_confidence_ranks_a_full_comparison_above_a_thin_one() -> None:
    target = features(
        facets={"mechanics": {"platforming"}, "setting": {"fantasy"}, "style": {"3d"}},
        genres={"platformer"},
        developer="studio-a",
        release_year=2026,
        metascore=80,
    )
    # Knows nothing but its genre, which happens to match perfectly.
    thin = features(genres={"platformer"})
    # Shares most of the feature set, but not all of it perfectly.
    full = features(
        facets={"mechanics": {"platforming"}, "setting": {"fantasy"}, "style": {"2d"}},
        genres={"platformer"},
        developer="studio-b",
        release_year=2025,
        metascore=76,
    )

    assert compare(target, thin).raw > compare(target, full).raw  # thin looks perfect
    assert compare(target, full).score > compare(target, thin).score  # confidence decides


def test_metacritic_peer_listing_counts_as_a_signal() -> None:
    left = features(slug="game-a", related_slugs={"game-b"}, genres={"rpg"})
    right = features(slug="game-b", related_slugs=set(), genres={"rpg"})
    stranger = features(slug="game-c", related_slugs=set(), genres={"rpg"})

    assert compare(left, right).score > compare(left, stranger).score
    names = [component.name for component in compare(left, right).components]
    assert "related" in names


def test_age_rating_is_compared_when_both_games_have_one() -> None:
    same = compare(features(esrb_rating="M"), features(esrb_rating="M"))
    different = compare(features(esrb_rating="M"), features(esrb_rating="E"))
    missing = compare(features(esrb_rating="M"), features())

    assert same.raw == 1.0
    assert different.raw == 0.0
    assert [component.name for component in missing.components] == []
