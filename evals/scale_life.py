"""Generate a coherent, dense fictional life as a stream of conversation turns.

The goal is scale realism: a large interconnected cast (people, places,
projects) developed over simulated months, with the messiness a real vault
accumulates — recurring characters, evolving facts, corrections, name
variants, relationships, feelings, plans. Driven through the real pipeline,
this exercises dedup, entity resolution, narrative growth, and supersession
at volume.

Names are invented (no real people). Deterministic with a seed so a run is
reproducible.
"""
from __future__ import annotations

import random

FIRST = [
    "Corwin", "Delia", "Ansel", "Marisol", "Thaddeus", "Odette", "Griffin", "Isolde",
    "Barnaby", "Selene", "Everett", "Priya", "Lucian", "Wren", "Silas", "Nadia",
    "Ozzie", "Fenna", "Rourke", "Calla", "Dmitri", "Saoirse", "Hollis", "Yuki",
    "Ignatius", "Beatrix", "Tobias", "Mireille", "Caspar", "Rosalind", "Emeric", "Juniper",
    "Ravi", "Cordelia", "Bishop", "Aurelia", "Finnegan", "Talullah", "Zephyr", "Magnolia",
]
LAST = [
    "Ashcroft", "Vandermeer", "Okonkwo", "Ferreira", "Blackwood", "Nakamura", "Delacroix",
    "Petrov", "Hargrove", "Espinoza", "Whitlock", "Bauer", "Castellanos", "Lindqvist",
    "Abernathy", "Rosenthal", "Mbeki", "Fontaine", "Sturgess", "Kowalski",
]
PLACES = [
    "the Blue Heron Diner", "Cedar Ridge", "the Old Foundry", "Marlowe Street",
    "the Tidewater Marina", "Kestrel Park", "the Ironwood Cabin", "Sutton Hollow",
    "the Larkspur Cafe", "Northgate Studios", "the Ferndale Library", "Copperfield Hall",
]
PROJECTS = [
    "the greenhouse rebuild", "the community radio station", "the trail-mapping app",
    "the vintage motorcycle restoration", "the neighborhood tool library",
    "the oral-history archive", "the rooftop bee project", "the model railroad",
    "the letterpress zine", "the river cleanup",
]
ROLES = [
    "an old college friend", "a bandmate", "my neighbor", "a coworker", "my dentist",
    "a fellow volunteer", "my sister's ex", "the guy who runs the hardware store",
    "a friend from the gym", "someone from my book club", "my kids' soccer coach",
    "a musician I met at an open mic",
]
TRAITS = [
    "always running late", "obsessed with sourdough", "a terrible driver but a great cook",
    "quietly generous", "a bit of a conspiracy theorist", "the calmest person I know",
    "restoring an old sailboat", "learning the accordion", "training for a marathon",
    "going through a rough divorce", "between jobs right now", "new to the area",
]


def _person(rng):
    return f"{rng.choice(FIRST)} {rng.choice(LAST)}"


def generate(n_turns: int = 200, seed: int = 7) -> list[str]:
    rng = random.Random(seed)
    cast: list[str] = []
    turns: list[str] = []

    def ensure_cast(k=1):
        while len(cast) < k:
            cast.append(_person(rng))

    ensure_cast(6)

    templates_intro = [
        "I want to tell you about {p}, {role}. They're {trait}.",
        "Met up with {p} today — {role}, {trait}.",
        "{p} came by. You should know they're {role} and {trait}.",
    ]
    templates_update = [
        "{p} has been on my mind. Things have gotten harder for them lately.",
        "Good news about {p} — the thing they were worried about worked out.",
        "{p} and I talked for hours about {proj}.",
        "Ran into {p} at {place}. We go way back.",
        "{p} is helping me with {proj} now.",
        "{p} introduced me to {p2} last week.",
        "{p} and {p2} finally patched things up after that falling out.",
        "Bad week for {p}. I'm worried about them.",
        "{p} pulled through. Honestly didn't think they would.",
    ]
    templates_place = [
        "Spent the afternoon at {place}. It always centers me.",
        "{place} is where {p} and I first met, years ago.",
        "We're moving {proj} over to {place} next month.",
    ]
    templates_project = [
        "Made real progress on {proj} this weekend.",
        "{proj} hit a wall. Might have to start over.",
        "{proj} is finally coming together thanks to {p}.",
    ]
    templates_correction = [
        "Correction — I said {p} was {role}, but really they're {role2}.",
        "Actually {p}'s last name isn't what I told you; it's {last}.",
        "I was wrong earlier: {proj} is happening at {place}, not where I said.",
    ]
    templates_feeling = [
        "Feeling stretched thin between {proj} and everything with {p}.",
        "Grateful for {p} today. Not everyone shows up like that.",
        "Some days {proj} feels pointless, then {p} reminds me why it matters.",
    ]
    templates_plan = [
        "Reminder: coffee with {p} next Tuesday.",
        "Planning a work day for {proj} at {place} on Saturday.",
        "{p}'s birthday is coming up. I should do something.",
    ]

    buckets = (
        templates_update * 4 + templates_place * 2 + templates_project * 3
        + templates_correction + templates_feeling * 2 + templates_plan * 2
    )

    for i in range(n_turns):
        # steadily grow the cast so the vault keeps gaining new entities
        if i % 4 == 0 and len(cast) < 45:
            p = _person(rng)
            cast.append(p)
            t = rng.choice(templates_intro).format(p=p, role=rng.choice(ROLES), trait=rng.choice(TRAITS))
            turns.append(t)
            continue
        ensure_cast(2)
        p, p2 = rng.sample(cast, 2)
        t = rng.choice(buckets).format(
            p=p, p2=p2, role=rng.choice(ROLES), role2=rng.choice(ROLES),
            proj=rng.choice(PROJECTS), place=rng.choice(PLACES),
            last=rng.choice(LAST), trait=rng.choice(TRAITS),
        )
        turns.append(t)
    return turns


if __name__ == "__main__":
    import sys

    n = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    for t in generate(n):
        print(t)
