# utils/copyright_filter.py
from __future__ import annotations

import logging
import os
import re
from typing import Optional

log = logging.getLogger("copyright_filter")

_COPYRIGHT_AI_SCREEN_ENABLED = os.getenv("COPYRIGHT_AI_SCREEN_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "y", "on",
}

# ---------------------------------------------------------------------------
# Layer 1 – Static keyword blocklist
# ---------------------------------------------------------------------------

BLOCKED_NAMES: set[str] = {
    # --- Anime / Manga ---
    "goku", "vegeta", "gohan", "frieza", "bulma", "krillin", "piccolo", "trunks",
    "naruto", "sasuke", "sakura", "kakashi", "itachi", "hinata", "gaara", "jiraiya",
    "luffy", "zoro", "nami", "sanji", "chopper", "robin", "usopp", "shanks",
    "ichigo", "rukia", "aizen", "byakuya",
    "gon", "killua", "hisoka", "kurapika",
    "eren", "mikasa", "levi", "armin",
    "edward elric", "alphonse elric",
    "light yagami", "ryuk", "misa amane",
    "sailor moon", "tuxedo mask",
    "inuyasha", "kagome",
    "natsu", "erza", "gray", "lucy heartfilia",
    "deku", "izuku midoriya", "bakugo", "todoroki", "all might", "ochako",
    "tanjiro", "nezuko", "zenitsu", "inosuke",
    "gojo", "itadori", "sukuna", "megumi",
    "asta", "yuno",
    "saitama", "genos",
    "rem", "emilia", "subaru natsuki",
    "hatsune miku", "miku",
    "asuka langley", "rei ayanami", "shinji ikari",
    # --- Video Games ---
    "mario", "luigi", "peach", "princess peach", "bowser", "toad", "yoshi",
    "link", "zelda", "princess zelda", "ganondorf",
    "pikachu", "charizard", "mewtwo", "eevee", "jigglypuff", "ash ketchum",
    "sonic", "tails", "knuckles", "shadow", "amy rose",
    "master chief", "cortana",
    "kratos", "atreus",
    "cloud strife", "tifa", "sephiroth", "aerith",
    "geralt", "ciri", "yennefer", "triss",
    "joel", "ellie",
    "steve", "alex", "creeper", "enderman",
    "kirby", "meta knight",
    "samus", "samus aran",
    "megaman", "mega man",
    "pac-man", "pacman",
    "ryu", "ken", "chun-li",
    "lara croft",
    "solid snake", "raiden",
    "dante", "vergil",
    "2b", "9s",
    # --- Western Animation / Comics ---
    "mickey mouse", "minnie mouse", "donald duck", "goofy",
    "bugs bunny", "daffy duck", "tweety",
    "spongebob", "patrick star", "squidward", "sandy cheeks",
    "homer simpson", "bart simpson", "marge simpson", "lisa simpson",
    "peter griffin", "stewie griffin",
    "rick sanchez", "morty smith",
    "finn", "jake the dog",
    "steven universe", "garnet",
    "aang", "katara", "zuko", "sokka", "toph", "azula", "korra",
    "ben tennyson", "ben 10",
    "shaggy", "scooby", "scooby-doo",
    "tom", "jerry",
    "superman", "batman", "wonder woman", "aquaman", "the flash",
    "spider-man", "spiderman", "peter parker", "miles morales",
    "iron man", "tony stark", "captain america", "steve rogers",
    "thor", "hulk", "bruce banner", "black widow", "natasha romanoff",
    "wolverine", "deadpool", "thanos", "loki",
    "joker", "harley quinn", "catwoman",
    "shrek", "donkey",
    "elsa", "anna", "olaf",
    "simba", "mufasa", "scar",
    "woody", "buzz lightyear",
    "nemo", "dory",
    "ratatouille", "remy",
    "wall-e",
    "jack skellington",
    # --- Movies / TV ---
    "darth vader", "luke skywalker", "han solo", "yoda", "obi-wan",
    "gandalf", "frodo", "aragorn", "legolas", "gollum", "sauron",
    "harry potter", "hermione", "ron weasley", "dumbledore", "voldemort", "snape",
    "katniss", "katniss everdeen",
    "jack sparrow",
    "john wick",
    "james bond",
    "sherlock holmes",
    "the doctor",
    "eleven",
    "walter white", "heisenberg", "jesse pinkman",
    "daenerys", "jon snow", "tyrion",
    # --- Public Figures ---
    "elon musk", "jeff bezos", "mark zuckerberg", "bill gates",
    "donald trump", "joe biden", "barack obama",
    "taylor swift", "beyonce", "rihanna", "drake", "kanye west",
    "kim kardashian", "kylie jenner",
    "pewdiepie", "mrbeast", "mr beast",
    "lebron james", "michael jordan", "cristiano ronaldo", "lionel messi",
    "oprah", "ellen degeneres",
    "keanu reeves", "dwayne johnson", "the rock",
    "ariana grande", "billie eilish", "ed sheeran",
    "andrew tate", "logan paul", "jake paul",
    "ninja", "pokimane", "markiplier", "jacksepticeye",
}

BLOCKED_FRANCHISES: set[str] = {
    "pokemon", "pokémon", "digimon",
    "dragon ball", "dragonball",
    "one piece",
    "naruto", "bleach", "fairy tail",
    "my hero academia", "boku no hero",
    "attack on titan", "shingeki no kyojin",
    "demon slayer", "kimetsu no yaiba",
    "jujutsu kaisen",
    "fullmetal alchemist",
    "death note",
    "sword art online",
    "hunter x hunter",
    "neon genesis evangelion",
    "disney", "pixar", "dreamworks",
    "marvel", "avengers", "x-men",
    "dc comics", "justice league",
    "star wars", "star trek",
    "lord of the rings", "lotr", "middle-earth",
    "harry potter", "hogwarts", "wizarding world",
    "game of thrones", "westeros",
    "hunger games",
    "nintendo", "super smash",
    "playstation", "xbox",
    "minecraft", "roblox", "fortnite",
    "league of legends", "valorant",
    "genshin impact", "hoyoverse",
    "final fantasy",
    "the witcher",
    "call of duty",
    "grand theft auto", "gta",
    "the last of us",
    "resident evil",
    "metal gear",
    "kingdom hearts",
    "persona",
    "dark souls", "elden ring",
    "overwatch",
    "apex legends",
    "rick and morty",
    "the simpsons",
    "family guy",
    "south park",
    "avatar the last airbender", "avatar: the last airbender",
    "spongebob", "nickelodeon",
    "cartoon network",
    "transformers",
    "power rangers",
    "studio ghibli", "hayao miyazaki",
    "sailor moon",
    "cowboy bebop",
    "hololive", "vtuber",
    "coca-cola", "pepsi", "mcdonalds",
    "nike", "adidas",
    "apple inc", "google", "microsoft", "amazon",
    "tesla", "spacex",
}


def _normalize(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").strip().lower())


def check_copyright_blocklist(
    display_name: str,
    character_id: str,
    description: str,
    prompt: str,
) -> Optional[str]:
    """Returns a rejection reason if any field matches the blocklist, else None."""
    name_norm = _normalize(display_name)
    cid_norm = _normalize(character_id)
    desc_norm = _normalize(description)
    prompt_norm = _normalize(prompt)

    identity_fields = (name_norm, cid_norm)
    all_fields = (name_norm, cid_norm, desc_norm, prompt_norm)

    for blocked in BLOCKED_NAMES:
        b = _normalize(blocked)
        for field in identity_fields:
            if b == field or b in field.split():
                return (
                    f"The name **{blocked}** is associated with a copyrighted character or public figure. "
                    "Custom characters must be entirely original."
                )
            if len(b) > 3 and b in field:
                return (
                    f"The name contains **{blocked}**, which is associated with a copyrighted character "
                    "or public figure. Custom characters must be entirely original."
                )

    for franchise in BLOCKED_FRANCHISES:
        f = _normalize(franchise)
        for field in all_fields:
            if f in field:
                return (
                    f"References to **{franchise}** are not allowed. "
                    "Custom characters must not be based on any copyrighted franchise or trademarked property."
                )

    return None


# ---------------------------------------------------------------------------
# Layer 2 – AI-powered screening
# ---------------------------------------------------------------------------

_AI_SYSTEM_PROMPT = """\
You are a content compliance screener. Your task is to determine whether a \
proposed fictional character is based on, derived from, or intended to represent:
1. A real person (living or deceased), public figure, or celebrity
2. A copyrighted fictional character from any franchise (anime, games, movies, TV, comics, etc.)
3. A trademarked property, brand, or entity

You will receive the character's name, description, and personality prompt. \
Analyze ALL fields together for combined intent (e.g., a character named "Kakarot" \
with a description mentioning "Saiyan warrior" is clearly Goku from Dragon Ball).

Respond with EXACTLY one line:
- PASS  (if the character appears to be entirely original)
- FAIL: <short reason>  (if the character appears to reference a real person, copyrighted character, or trademark)

Be strict. When in doubt, flag it.\
"""


async def ai_copyright_screen(
    display_name: str,
    description: str,
    prompt: str,
) -> tuple[bool, str]:
    """Returns (is_flagged, reason). On error, returns (False, '') so submission falls through to manual review."""
    if not _COPYRIGHT_AI_SCREEN_ENABLED:
        return False, ""

    try:
        from config import OPENAI_API_KEY, OPENAI_BASE_URL, OPENAI_MODEL_FREE, OPENAI_TIMEOUT_S

        if not OPENAI_API_KEY:
            return False, ""

        import httpx

        user_content = (
            f"Character Name: {display_name}\n"
            f"Description: {description[:800]}\n"
            f"Personality Prompt: {prompt[:1500]}"
        )

        async with httpx.AsyncClient(timeout=float(OPENAI_TIMEOUT_S)) as client:
            resp = await client.post(
                f"{OPENAI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={
                    "model": OPENAI_MODEL_FREE,
                    "messages": [
                        {"role": "system", "content": _AI_SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
                    ],
                    "max_tokens": 120,
                    "temperature": 0.0,
                },
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"].strip()

        if text.upper().startswith("FAIL"):
            reason = text.split(":", 1)[1].strip() if ":" in text else "Matched a protected character or person."
            return True, reason

        return False, ""

    except Exception:
        log.warning("AI copyright screening failed; falling through to manual review", exc_info=True)
        return False, ""
