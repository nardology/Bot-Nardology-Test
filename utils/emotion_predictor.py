from __future__ import annotations

"""Tiny, *robust* emotion classifier for /talk image selection.

This used to use raw regex patterns, but some patterns (or user-provided text) can
accidentally produce invalid regex (e.g. lone '+' or '*'), which can crash /talk
and leave Discord interactions stuck in the "thinking" state.

We now treat *most* keywords as **literal substrings** (case-insensitive).
Only a very small handful of patterns are kept as real regex for things like
"???" punctuation emphasis.
"""

from typing import Dict, List, Tuple
import re

# Keywords are checked as case-insensitive *substrings* unless prefixed with 're:'.
_KEYWORDS: Dict[str, List[str]] = {
    "happy": [
        "happy", "glad", "joy", "joyful", "excited", "thrilled", "delighted",
        "lol", "lmao", "rofl", "haha", "hehe", "yay", "woo",
        "😊", "😄", "😁", "😆", "😃",
        ":-)", ":)", "=)", ":D", ":-D",
    ],
    "sad": [
        "sad", "down", "cry", "crying", "tear", "tears", "depressed", "mourn",
        "heartbroken", "lonely", "miss you", "grief",
        "😢", "😭", "☹", "🙁", "😞",
        ":-(", ":(", "='(", ":'(",
    ],
    "angry": [
        "angry", "mad", "furious", "rage", "pissed", "annoyed", "irritated",
        "hate", "screw you", "shut up", "stupid", "idiot",
        "😡", "🤬", "😠",
        ">:(", "ಠ_ಠ",
    ],
    "scared": [
        "scared", "afraid", "terrified", "fear", "panic", "panicking",
        "nervous", "anxious", "worried",
        "😨", "😰", "😱",
    ],
    "confused": [
        "confused", "what", "huh", "uh", "umm", "i don't know", "idk",
        "🤔", "😕", "😵‍💫",
        # real regex: lots of question marks ("???" etc)
        "re:\\?{3,}",
    ],
    "neutral": [
        "okay", "ok", "fine", "sure", "alright",
        "😐", "🙂",
    ],
}

_COMPILED: Dict[str, List[Tuple[bool, re.Pattern[str] | str]]] = {}

def _compile():
    compiled: Dict[str, List[Tuple[bool, re.Pattern[str] | str]]] = {}
    for emotion, pats in _KEYWORDS.items():
        out: List[Tuple[bool, re.Pattern[str] | str]] = []
        for p in pats:
            if p.startswith("re:"):
                raw = p[3:]
                # If a regex is invalid, fall back to a literal.
                try:
                    out.append((True, re.compile(raw, flags=re.IGNORECASE)))
                except re.error:
                    out.append((False, raw.lower()))
            else:
                out.append((False, p.lower()))
        compiled[emotion] = out
    return compiled

_COMPILED = _compile()

def detect_topics(
    user_text: str,
    topic_reactions: dict[str, str] | None,
) -> list[tuple[str, str]]:
    """Scan *user_text* for topics defined in *topic_reactions*.

    Returns a list of ``(topic, reaction)`` pairs whose keywords all
    appear in the user's message (case-insensitive).
    """
    if not topic_reactions or not user_text:
        return []
    lower = user_text.lower()
    hits: list[tuple[str, str]] = []
    for topic, reaction in topic_reactions.items():
        words = topic.lower().split()
        if not words:
            continue
        if len(words) == 1:
            if words[0] in lower:
                hits.append((topic, reaction))
        else:
            if all(w in lower for w in words):
                hits.append((topic, reaction))
    return hits


def predict_emotion(style_id: str, text: str | None = None, *, user_prompt: str | None = None) -> str:
    """Return an emotion key: happy/sad/angry/scared/confused/neutral.

    Backwards compatible:
    - older callers used predict_emotion(style_id, text)
    - newer callers may pass predict_emotion(style_id, user_prompt=...)
    """
    if text is None:
        text = user_prompt or ""
    if not text:
        return "neutral"
    t = text.lower()

    # Priority order: strong emotions first.
    for emotion in ("angry", "scared", "sad", "happy", "confused"):
        for is_regex, item in _COMPILED.get(emotion, []):
            if is_regex:
                assert isinstance(item, re.Pattern)
                if item.search(text):
                    return emotion
            else:
                assert isinstance(item, str)
                if item and item in t:
                    return emotion

    return "neutral"
