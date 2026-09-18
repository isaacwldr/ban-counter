import json
import re
from dataclasses import dataclass, field

from ollama import chat


MODEL = "qwen3:8b"

VALID_ACTIONS = {
    "ban",
    "count",
    "leaderboard",
    "cooldown",
    "stats",
    "none",
}

VALID_STATS = {
    "most_banned_week",
    "top_accusers",
    "super_bans",
    "self_bans",
    "recent_activity",
}


@dataclass
class MessageIntent:
    action: str = "none"
    targets: list[str] = field(default_factory=list)
    super_ban: bool = False
    stat_type: str | None = None


SYSTEM_PROMPT = """
You are the language parser for a humorous Discord ban-counter bot called TAGINA.

Your ONLY job is to classify a ban-counter-related Discord message into structured JSON.
You are not a general chatbot.

"Ban Bot" and "Tagina" are invocation names. Never return either as a target when
they are being used to address the bot.

Possible actions:
- "ban": user is requesting fictional ban points for one or more people
- "count": user wants the current ban-point total for one or more people
- "leaderboard": user wants the all-time ban leaderboard
- "cooldown": user wants to know whether their Super Ban is ready or how long remains
- "stats": user wants one of the supported ban statistics
- "none": unrelated, negated, conversational, or unsupported request

For action="stats", stat_type must be one of:
- "most_banned_week": most ban points received during the last 7 days
- "top_accusers": users who have submitted the most distinct ban requests
- "super_bans": number of Super Bans received by a target
- "self_bans": number of times a target requested a ban on themselves
- "recent_activity": recent ban-counter activity

Examples:

"ban conner"
{"action":"ban","targets":["conner"],"super_ban":false,"stat_type":null}

"I invoke the ancient rite of the super ban upon CJ"
{"action":"ban","targets":["CJ"],"super_ban":true,"stat_type":null}

"how many bans does Tyler have"
{"action":"count","targets":["Tyler"],"super_ban":false,"stat_type":null}

"show me the ban leaderboard"
{"action":"leaderboard","targets":[],"super_ban":false,"stat_type":null}

"can I super ban yet?"
{"action":"cooldown","targets":[],"super_ban":false,"stat_type":null}

"who got banned the most this week?"
{"action":"stats","targets":[],"super_ban":false,"stat_type":"most_banned_week"}

"who hands out the most bans?"
{"action":"stats","targets":[],"super_ban":false,"stat_type":"top_accusers"}

"how many super bans has CJ eaten?"
{"action":"stats","targets":["CJ"],"super_ban":false,"stat_type":"super_bans"}

"how many times has CJ banned himself?"
{"action":"stats","targets":["CJ"],"super_ban":false,"stat_type":"self_bans"}

"what happened recently?"
{"action":"stats","targets":[],"super_ban":false,"stat_type":"recent_activity"}

"don't ban Conner"
{"action":"none","targets":[],"super_ban":false,"stat_type":null}

"what are we playing tonight?"
{"action":"none","targets":[],"super_ban":false,"stat_type":null}

FOLLOW-UP CONTEXT:
The application may provide a small recent_context object containing only structured,
temporary state from the user's previous TAGINA request. Use it only when the current
message is obviously a follow-up.

Example:
recent_context={"action":"count","targets":["Tyler"]}
current_message="what about CJ?"
=> {"action":"count","targets":["CJ"],"super_ban":false,"stat_type":null}

Never inherit a previous "ban" action from context. A vague follow-up must never create
a new ban request. Context may only help with informational requests such as count or stats.

Do NOT decide whether an action is allowed.
Do NOT apply cooldowns.
Do NOT modify ban counts.
Do NOT select random Discord users.
Do NOT enforce application rules.
Preserve names and nicknames from the current message as targets.
Return JSON only.
"""

AI_TRIGGER_PATTERN = r"\b(?:ban bot|tagina)\b[,:]?\s*"


def strip_ai_trigger(content: str) -> str:
    return re.sub(
        AI_TRIGGER_PATTERN,
        "",
        content,
        count=1,
        flags=re.IGNORECASE,
    ).strip()


def interpret_message(
    content: str,
    context: dict | None = None,
) -> MessageIntent:
    payload = {
        "current_message": strip_ai_trigger(content),
    }

    if context:
        payload["recent_context"] = context

    response = chat(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": json.dumps(payload),
            },
        ],
        format="json",
        think=False,
        keep_alive=-1,
        options={
            "temperature": 0,
        },
    )

    try:
        data = json.loads(response.message.content)

        action = data.get("action", "none")
        if action not in VALID_ACTIONS:
            action = "none"

        targets = data.get("targets", [])
        if not isinstance(targets, list):
            targets = []

        targets = [
            target.strip()
            for target in targets
            if isinstance(target, str) and target.strip()
        ][:5]

        super_ban = data.get("super_ban", False)
        if not isinstance(super_ban, bool):
            super_ban = False

        stat_type = data.get("stat_type")
        if stat_type not in VALID_STATS:
            stat_type = None

        if action != "stats":
            stat_type = None

        if action != "ban":
            super_ban = False

        return MessageIntent(
            action=action,
            targets=targets,
            super_ban=super_ban,
            stat_type=stat_type,
        )

    except (json.JSONDecodeError, TypeError, AttributeError):
        return MessageIntent()
