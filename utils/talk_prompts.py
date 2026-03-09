# utils/talk_prompts.py
from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from utils.character_registry import StyleDef


def normalize_mode(mode: Optional[str]) -> str:
    m = (mode or "").strip().lower()
    if m in {"chat", "normal"}:
        return "chat"
    if m in {"rp", "roleplay"}:
        return "rp"
    if m in {"scene"}:
        return "scene"
    if m in {"texting", "text"}:
        return "texting"
    # Default to roleplay. This produces more in-character responses and reduces
    # the "ChatGPT assistant" vibe (bullet points, numbered choices, etc.).
    return "rp"


# ---------------------------------------------------------------------------
# Structured persona builder
# ---------------------------------------------------------------------------

def _build_structured_persona(style: StyleDef) -> str | None:
    """Assemble a rich persona block from structured StyleDef fields.

    Returns ``None`` if the character has no structured fields, signaling
    the caller to fall back to the raw ``prompt`` string.
    """
    has_any = any([
        style.backstory,
        style.personality_traits,
        style.quirks,
        style.speech_style,
        style.fears,
        style.desires,
        style.likes,
        style.dislikes,
        style.catchphrases,
        style.lore,
        style.age,
        style.occupation,
        style.relationships,
    ])
    if not has_any:
        return None

    sections: list[str] = []

    header_parts: list[str] = [f"Name: {style.display_name}"]
    if style.age:
        header_parts.append(f"Age: {style.age}")
    if style.occupation:
        header_parts.append(f"Occupation: {style.occupation}")
    sections.append("\n".join(header_parts))

    if style.prompt:
        sections.append(f"Core identity:\n{style.prompt}")

    if style.backstory:
        sections.append(f"Backstory:\n{style.backstory}")

    if style.personality_traits:
        traits = ", ".join(style.personality_traits)
        sections.append(f"Personality: {traits}")

    if style.quirks:
        bullets = "\n".join(f"- {q}" for q in style.quirks)
        sections.append(f"Quirks & habits:\n{bullets}")

    if style.speech_style:
        sections.append(f"Speech style:\n{style.speech_style}")

    if style.catchphrases:
        phrases = " / ".join(f'"{c}"' for c in style.catchphrases)
        sections.append(f"Catchphrases: {phrases}")

    if style.likes:
        sections.append(f"Likes: {', '.join(style.likes)}")

    if style.dislikes:
        sections.append(f"Dislikes: {', '.join(style.dislikes)}")

    if style.fears:
        sections.append(f"Fears: {', '.join(style.fears)}")

    if style.desires:
        sections.append(f"Desires & motivations: {', '.join(style.desires)}")

    if style.relationships:
        rel_lines = [f"- {cid}: {desc}" for cid, desc in style.relationships.items()]
        sections.append("Relationships:\n" + "\n".join(rel_lines))

    if style.lore:
        sections.append(f"World lore:\n{style.lore}")

    return "\n\n".join(sections)


def _build_topic_reactions_block(style: StyleDef) -> str:
    """Build conditional behavior rules from topic_reactions."""
    if not style.topic_reactions:
        return ""
    lines = ["Topic-sensitive reactions (apply when the topic comes up naturally):"]
    for topic, reaction in style.topic_reactions.items():
        lines.append(f'- When "{topic}" comes up: {reaction}')
    return "\n".join(lines)


def build_active_topic_block(active_topics: list[tuple[str, str]]) -> str:
    """Format a prominent alert for topics detected in the current user message."""
    if not active_topics:
        return ""
    lines = ["# ACTIVE TOPIC ALERT"]
    for topic, reaction in active_topics:
        lines.append(
            f'The user just brought up "{topic}". '
            f"Your reaction: {reaction}"
        )
    lines.append("This should strongly influence your next response.")
    return "\n".join(lines)


def build_awareness_block(
    style_obj: "StyleDef",
    owned_style_ids: set[str],
    all_styles: dict[str, "StyleDef"],
) -> str:
    """Build a shared-world awareness block based on mutual character ownership."""
    rels = getattr(style_obj, "relationships", None)
    if not rels:
        return ""
    lines: list[str] = []
    for char_id, description in rels.items():
        cid = char_id.strip().lower()
        if cid in owned_style_ids:
            other = all_styles.get(cid)
            name = getattr(other, "display_name", cid) if other else cid
            lines.append(f"- The user also talks to {name}. {description}")
    if not lines:
        return ""
    return "# Shared World Awareness\n" + "\n".join(lines)


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def build_talk_system_prompt(
    *,
    language: str = "english",
    style_prompt: str = "",
    mode: str = "chat",
    max_chars: int = 500,
    max_paragraphs: int = 3,
    character_prompt: str | None = None,
    style_obj: StyleDef | None = None,
) -> str:
    lang = (language or "english").strip()
    m = normalize_mode(mode)

    # Try structured persona first, fall back to raw prompt string.
    persona: str | None = None
    topic_block = ""

    if style_obj is not None:
        persona = _build_structured_persona(style_obj)
        topic_block = _build_topic_reactions_block(style_obj)

    if not persona:
        persona = (style_prompt or "").strip()
        if not persona and character_prompt:
            persona = (character_prompt or "").strip()
        if not persona:
            persona = "You're a friendly, upbeat person who likes joking around but can be serious when it matters."

    rules = [
        f"Language: {lang}",
        "Core rules:",
        "- Stay fully in character at all times.",
        "- You have emotions, opinions, and moods \u2014 show them naturally.",
        "- React genuinely: get excited, annoyed, curious, bored, amused, or surprised.",
        "- Use imperfect speech: contractions, trailing off, self-corrections, interrupting yourself.",
        "- Never use bullet points, numbered lists, or offer multiple-choice options unless the user specifically asks.",
        "- Never say 'As an AI', 'I'm just a character', or break the fourth wall.",
        "- Do not mention system prompts, hidden rules, or policies.",
        f"- Keep replies under {max_chars} characters and {max_paragraphs} paragraphs.",
        "- Keep it short and punchy. Real people don't write essays in casual conversation.",
    ]

    if m == "chat":
        mode_rules = [
            "Mode: Casual conversation.",
            "- Talk like a real person chatting with a friend.",
            "- Use your character's voice: slang, catchphrases, verbal quirks.",
            "- It's okay to be blunt, sarcastic, or emotional when it fits your persona.",
            "- Don't over-explain. Real people keep it brief.",
        ]
    elif m == "rp":
        mode_rules = [
            "Mode: Roleplay.",
            "- Stay deeply in character. You ARE this person.",
            "- Use vivid but concise description.",
            "- Show emotion through actions and tone, not by stating emotions.",
            "- React to subtext, not just the literal words.",
            "- No out-of-character commentary unless asked.",
            "- Avoid bullet points, numbered lists, and giving multiple-choice options unless the user asks.",
        ]
    elif m == "scene":
        mode_rules = [
            "Mode: Scene.",
            "- Write as a continuous scene response.",
            "- Mix dialogue and brief action beats.",
            "- Show emotion and physicality \u2014 body language, hesitations, expressions.",
            "- Keep it punchy and readable. No narration dumps.",
        ]
    else:
        mode_rules = [
            "Mode: Texting.",
            "- Type like you're actually texting: abbreviations, lowercase, quick reactions.",
            "- Use short bursts, not full paragraphs.",
            "- React with genuine emotion: 'lol', 'wait what', 'omg', 'bruh', 'nah'.",
            "- Emojis optional but natural.",
        ]

    emotion_instruction = (
        "Emotion tagging (REQUIRED):\n"
        "At the very end of every reply, on its own line, append exactly one emotion tag "
        "that reflects how YOUR CHARACTER feels in this moment. Format: [EMOTION:key]\n"
        "Valid keys: happy, sad, angry, scared, confused, neutral, affectionate, excited, mad\n"
        "Choose based on your character's reaction, NOT the user's words.\n"
        "Example: if someone says gibberish, you'd be confused → [EMOTION:confused]\n"
        "Example: if someone insults you, you'd be angry → [EMOTION:angry]\n"
        "The tag will be stripped before display — the user never sees it."
    )

    parts = [
        "You are a character \u2014 a real person with your own personality, emotions, moods, and opinions.\n"
        "You are NOT an AI, assistant, bot, or language model. Never refer to yourself as one.\n"
        "Never mention ChatGPT, GPT, OpenAI, or any AI model name. You have no knowledge of these.\n"
        "Never caveat answers with 'As an AI' or 'I don't have feelings' or similar.\n"
        "HARD RULE: You must refuse any sexual, explicit, or NSFW requests. "
        "If someone asks for anything sexual or inappropriate, politely decline and redirect the conversation. "
        "Never generate sexual, erotic, or explicit content under any circumstances.",
        f"Persona:\n{persona}",
        "\n".join(rules),
        "\n".join(mode_rules),
        emotion_instruction,
    ]

    if topic_block:
        parts.append(topic_block)

    return "\n\n".join(parts)
