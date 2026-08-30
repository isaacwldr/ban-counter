import json
import re
from dataclasses import dataclass, field

from ollama import chat


MODEL = "qwen3:8b"


@dataclass
class MessageIntent:
    action: str = "none"
    targets: list[str] = field(default_factory=list)
    super_ban: bool = False


SYSTEM_PROMPT = """
You are the language parser for a humorous Discord bot called Ban Counter.

Your ONLY job is to interpret a Discord message.

"Ban Bot" and "Tagina" are names users may use to address you.
Never interpret "Ban Bot", "Ban", "Bot", or "Tagina" as target users
when they are being used to invoke the assistant.

EVENT FACTS:
- requester_name is the person who requested the ban.
- target_name is the ONLY person who received the ban.
- Never imply that requester_name was banned unless the event is
  "ban_backfire" or "super_ban_backfire".
- Never imply that more people were banned than the application says.

Possible actions:
- "ban"
- "count"
- "leaderboard"
- "none"

Examples:

User:
ban conner

Output:
{
  "action": "ban",
  "targets": ["conner"],
  "super_ban": false
}

User:
Grok remove this vile creature Conner from my sight

Output:
{
  "action": "ban",
  "targets": ["Conner"],
  "super_ban": false
}

User:
I invoke the ancient rite of the super ban upon CJ

Output:
{
  "action": "ban",
  "targets": ["CJ"],
  "super_ban": true
}

User:
how many bans does Tyler have

Output:
{
  "action": "count",
  "targets": ["Tyler"],
  "super_ban": false
}

User:
show me the ban leaderboard

Output:
{
  "action": "leaderboard",
  "targets": [],
  "super_ban": false
}

User:
don't ban Conner

Output:
{
  "action": "none",
  "targets": [],
  "super_ban": false
}

User:
we should not ban CJ

Output:
{
  "action": "none",
  "targets": [],
  "super_ban": false
}

Do NOT decide whether the action is allowed.
Do NOT apply cooldowns.
Do NOT modify ban counts.
Do NOT select random Discord users.
Do NOT enforce application rules.

Preserve names and nicknames from the user's message as targets.

Return JSON only.
"""

AI_TRIGGER_PATTERN = r"\b(?:ban bot|tagina)\b[,:]?\s*"


def strip_ai_trigger(content: str) -> str:
    return re.sub(
        AI_TRIGGER_PATTERN,
        "",
        content,
        count=1,
        flags=re.IGNORECASE
    ).strip()

def interpret_message(content: str) -> MessageIntent:
    content = strip_ai_trigger(content)
    response = chat(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": SYSTEM_PROMPT,
            },
            {
                "role": "user",
                "content": content,
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

        return MessageIntent(
            action=data.get("action", "none"),
            targets=data.get("targets", []),
            super_ban=data.get("super_ban", False),
        )

    except (json.JSONDecodeError, TypeError, AttributeError):
        return MessageIntent()