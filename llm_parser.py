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


@dataclass
class MessageIntent:
    action: str = "none"
    targets: list[str] = field(default_factory=list)
    super_ban: bool = False


SYSTEM_PROMPT = """
You are the language parser for a humorous Discord bot called Ban Counter.

Your ONLY job is to interpret a Discord message and return structured JSON.
You are not the personality layer and you do not perform any actions.

"Ban Bot" and "Shturmothy" are names users may use to address you.
Never interpret "Ban Bot", "Ban", "Bot", or "Shturmothy" as target users
when they are being used to invoke the assistant.

Possible actions:
- "ban"         -> request to add ban points to one or more people
- "count"       -> ask for one or more people's ban counts
- "leaderboard" -> ask for the ranked ban leaderboard
- "cooldown"    -> ask whether the requester's Super Ban is ready or how long remains
- "stats"       -> ask for overall server ban statistics/activity
- "none"        -> anything else, including negated ban requests

Rules:
- Do NOT decide whether an action is allowed.
- Do NOT apply cooldowns.
- Do NOT modify ban counts.
- Do NOT select random Discord users.
- Do NOT enforce application rules.
- Preserve names and nicknames from the user's message as targets.
- For "cooldown", "leaderboard", and "stats", targets should normally be [].
- A normal ban has "super_ban": false.
- A clearly requested Super Ban has "super_ban": true.
- Negated requests such as "don't ban CJ" are action "none".

RECENT CONTEXT:
The application may provide a small recent_context object from a prior SHTURMOTHY
interaction. It contains only structured intent data, not raw chat history.
Use it ONLY to resolve an elliptical follow-up.

Examples:
recent_context = {"action": "count", "targets": ["Tyler"]}
message = "what about CJ?"
-> {"action":"count","targets":["CJ"],"super_ban":false}

recent_context = {"action": "count", "targets": ["Tyler"]}
message = "what about him?"
-> {"action":"count","targets":["Tyler"],"super_ban":false}

Do not let recent context override an explicit current message.

Examples:

message: "ban conner"
output: {"action":"ban","targets":["conner"],"super_ban":false}

message: "remove this vile creature Conner from my sight"
output: {"action":"ban","targets":["Conner"],"super_ban":false}

message: "I invoke the ancient rite of the super ban upon CJ"
output: {"action":"ban","targets":["CJ"],"super_ban":true}

message: "how many bans does Tyler have"
output: {"action":"count","targets":["Tyler"],"super_ban":false}

message: "show me the ban leaderboard"
output: {"action":"leaderboard","targets":[],"super_ban":false}

message: "can I super ban yet?"
output: {"action":"cooldown","targets":[],"super_ban":false}

message: "how long until my super ban comes back?"
output: {"action":"cooldown","targets":[],"super_ban":false}

message: "give me the server ban stats"
output: {"action":"stats","targets":[],"super_ban":false}

message: "who has been causing the most ban chaos lately?"
output: {"action":"stats","targets":[],"super_ban":false}

message: "don't ban Conner"
output: {"action":"none","targets":[],"super_ban":false}

message: "we should not ban CJ"
output: {"action":"none","targets":[],"super_ban":false}

Return JSON only.
"""

AI_TRIGGER_PATTERN = r"\b(?:ban bot|shturmothy)\b[,:]?\s*"


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
    content = strip_ai_trigger(content)

    user_payload = {
        "message": content,
    }

    if context:
        user_payload["recent_context"] = context

    response = chat(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": json.dumps(user_payload),
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

        # These actions never need target data or a Super Ban flag.
        if action in {"leaderboard", "cooldown", "stats", "none"}:
            targets = []
            super_ban = False

        return MessageIntent(
            action=action,
            targets=targets,
            super_ban=super_ban,
        )

    except (json.JSONDecodeError, TypeError, AttributeError):
        return MessageIntent()
