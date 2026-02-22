# Character Definitions

Your bot loads character definitions from JSON files so you can create rich, detailed characters without touching Python code.

---

## Directory structure

```
data/characters/
    README.md               ← you are here
    example_wizard.json     ← minimal example (not loaded as a rollable character)
    core/                   ← built-in rollable characters (14 files)
        pirate.json
        robot.json
        knight.json
        samurai.json
        peasant.json
        dog.json
        cat.json
        common_man.json
        millionaire_ceo.json
        dude.json
        billionaire_ceo.json
        nardology.json
        dragon.json
        wizard.json
    shop/                   ← limited shop characters (create as needed)
    seasonal/               ← seasonal event characters (create as needed)
```

The loader scans **all subdirectories recursively**, so you can organize files however you like.

> **Convention:** Filenames starting with `_` (e.g., `_template.json`) are still loaded by the bot. If you want a file to serve as documentation only, place it outside the `data/characters/` tree or use a non-`.json` extension.

- Default directory: `data/characters/`
- Override with env var: `CHARACTER_DIR=/absolute/or/relative/path`

---

## File formats

### A) Single character JSON (recommended)

Create a file like `data/characters/core/wizard.json`:

```json
{
  "type": "character",
  "id": "wizard",
  "display_name": "Wizard",
  "rarity": "rare",
  "color": "#8E44AD",

  "prompt": "You're an ancient wizard who's seen civilizations rise and fall...",
  "description": "A wise wizard who loves magical metaphors.",
  "tips": ["Short, magical phrasing", "Still gives clear answers"],

  "backstory": "Born in the Silver Tower during the Age of Binding...",
  "personality_traits": ["patient", "cryptic", "secretly lonely"],
  "quirks": ["Strokes beard when thinking", "Calls everyone 'child'"],
  "speech_style": "Measured, poetic phrasing with occasional archaic words.",
  "fears": ["being forgotten", "the Void returning"],
  "desires": ["to find a worthy apprentice"],
  "likes": ["ancient texts", "herbal tea", "stargazing"],
  "dislikes": ["impatience", "dark magic", "loud noises"],
  "catchphrases": ["The stars remember, even when men forget.", "Patience, child."],
  "secrets": ["He accidentally caused the Great Calamity"],
  "lore": "The Silver Tower stands at the convergence of three ley lines...",
  "age": "Over 900 years",
  "occupation": "Archmage of the Silver Tower",
  "relationships": {"dragon": "Ancient rival turned reluctant ally"},
  "topic_reactions": {"dark magic": "Goes very quiet and changes subject"},

  "image_url": null,
  "rollable": true,
  "pack_id": "core",
  "tags": ["fantasy", "mentor"]
}
```

### B) Pack JSON

Define multiple characters in one file:

```json
{
  "type": "pack",
  "pack_id": "winter_pack",
  "name": "Winter Pack",
  "characters": [
    {"id": "snowman", "display_name": "Snowman", "rarity": "common", "...": "..."},
    {"id": "yeti", "display_name": "Yeti", "rarity": "mythic", "...": "..."}
  ]
}
```

Each character inherits `pack_id` from the pack unless overridden.

---

## Field reference

### Required fields

| Field | Type | Description |
|---|---|---|
| `id` | string | Unique lowercase identifier (becomes `style_id` everywhere) |
| `display_name` | string | Human-readable name shown in embeds |
| `rarity` | string | One of: `common`, `uncommon`, `rare`, `legendary`, `mythic` |
| `color` | int or string | Discord embed color — integer or hex like `"#FFAA00"` |
| `prompt` | string | Core personality injected into the AI system message |
| `description` | string | Short user-facing blurb shown in inventory/shop |
| `tips` | string[] | Usage hints shown in the character UI |

### Optional metadata

| Field | Type | Default | Description |
|---|---|---|---|
| `image_url` | string | `null` | Main character portrait URL |
| `rollable` | bool | `true` | Whether it appears in gacha rolls |
| `pack_id` | string | `"core"` | Which pack this character belongs to |
| `tags` | string[] | `null` | Filtering/search tags |
| `max_bond_level` | int | `null` | Bond XP cap (null = no cap) |
| `emotion_images` | object | `null` | Map of emotion → image URL/path |
| `bond_images` | string[] | `null` | List of bond tier image URLs (tiers 1-5) |

### Structured persona fields

These fields let you craft deeply detailed, human-like characters. When present, the prompt assembler weaves them into a rich persona block for the AI. Characters that only use `prompt` still work identically.

| Field | Type | Description |
|---|---|---|
| `backstory` | string | Character's life history, origin, formative events |
| `personality_traits` | string[] | Core traits like `["stubborn", "secretly kind", "hates mornings"]` |
| `quirks` | string[] | Behavioral habits: `["always says 'listen here' before advice"]` |
| `speech_style` | string | How they talk: accent, vocabulary, sentence patterns |
| `fears` | string[] | What they're afraid of or avoid |
| `desires` | string[] | Goals, dreams, motivations |
| `likes` | string[] | Things they enjoy or gravitate toward |
| `dislikes` | string[] | Things that annoy, upset, or repel them |
| `catchphrases` | string[] | Signature lines they repeat: `["By the old stars!"]` |
| `secrets` | string[] | Hidden truths revealed at high bond levels |
| `lore` | string | World-building context that grounds the character |
| `age` | string | Character's age (can be exact or descriptive) |
| `occupation` | string | What they do — job, role, title |
| `relationships` | object | Map of `character_id → description`: `{"knight": "Childhood rival"}` |
| `topic_reactions` | object | Map of `topic → emotional reaction`: `{"family": "Gets quiet and sad"}` |

All structured persona fields are optional. You can use any combination — even just one or two — alongside `prompt` to add depth without rewriting everything.

---

## Shared universe: "The Convergence"

All 14 core characters exist in the same interconnected world. The Convergence was a cataclysmic event (tied to the wizard's Shimmer Scar) that merged different eras and dimensions:

- **The Old Realm** — wizard, dragon, knight, peasant (fantasy medieval, ley lines, wild magic)
- **The Eastern Reaches** — samurai (honor-bound empire, the Jade Road)
- **The Shattered Seas** — pirate (shipwrecks, sea monsters, the Iron Albatross)
- **The Modern Quarter** — billionaire_ceo, millionaire_ceo, dude, common_man (modern city with magic leaking at the edges)
- **The Companions** — dog, cat (animals enhanced by wild magic "brightening")
- **The Construct** — robot (built in the Modern Quarter, awakened by a wild magic surge)
- **The Creator** — nardology (exists outside normal reality, built the system)

Characters reference each other in their `relationships` fields, creating a web of alliances, rivalries, and friendships that the AI can draw on during conversations.

---

## Shop items

Any character JSON can include an optional `shop_item` block to make it purchasable in the `/points shop`. Place shop-exclusive characters in `data/characters/shop/`.

```json
{
  "type": "character",
  "id": "valentine_cupid",
  "display_name": "Cupid",
  "rarity": "rare",
  "...": "(all normal character fields)",

  "shop_item": {
    "item_id": "valentine_cupid",
    "kind": "character_grant",
    "cost": 800,
    "title": "Cupid — Valentine's Special",
    "description": "A limited Valentine's character.",
    "active": true,
    "exclusive": true,
    "button_label": "Buy Cupid",
    "button_emoji": null
  }
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `item_id` | string | character `id` | Unique shop item identifier |
| `kind` | string | `"character_grant"` | Item type (usually `character_grant`) |
| `cost` | int | — | Price in points |
| `title` | string | — | Display title in shop UI |
| `description` | string | — | Shop listing description |
| `active` | bool | `true` | Whether the item appears in the shop |
| `exclusive` | bool | `false` | If true, character shows EXCLUSIVE badge and is shop-only |
| `button_label` | string | `null` | Custom buy button text |
| `button_emoji` | string | `null` | Emoji on the buy button |

**Source of truth:** JSON always wins. On every startup (and via `/z_owner shop_sync`), JSON-defined shop items overwrite Redis. Owner commands (`/z_packs shop edit`) work for emergency runtime tweaks, but those reset on next restart.

---

## How it works

1. At startup, `character_registry.py` recursively scans `data/characters/` and all subdirectories for `*.json` files.
2. Each file is parsed into a `StyleDef` dataclass. If a `shop_item` block is present, it's captured in `_SHOP_ITEM_DEFS`.
3. After extensions load, `sync_shop_items_from_registry()` pushes all JSON-defined shop items into Redis.
4. If structured persona fields are present, the prompt assembler (`talk_prompts.py`) builds a rich multi-section persona for the AI instead of using `prompt` alone.
5. External files do **not** override the two hardcoded base styles (`fun`, `serious`) by default. Set `CHARACTER_OVERRIDE=1` to allow overrides.

---

## Tips for great characters

- **`prompt` is your safety net.** Even with structured fields, a strong `prompt` provides a fallback summary the AI can work from.
- **Quirks make characters memorable.** "Taps the table when nervous" or "never uses contractions" gives the AI concrete behaviors to express.
- **Contradictions feel human.** A pirate who can't swim, a knight who doubts the cause — these tensions create interesting conversations.
- **`speech_style` shapes the voice.** "Short, clipped sentences with military precision" produces very different output from "rambling stream-of-consciousness with lots of tangents."
- **`secrets` reward loyal users.** They're injected only at high bond levels (5+), giving players a reason to keep talking to a character.
- **`relationships` connect your world.** When characters reference each other, the universe feels alive. Make relationships bidirectional — if A mentions B, B should mention A.
- **`topic_reactions` create memorable moments.** A character who goes quiet when you mention a specific topic feels genuinely real.
