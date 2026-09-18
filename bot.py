from llm_parser import interpret_message
from personality import generate_tagina_response

import os
import re
import sqlite3
import discord
import json
import random
import asyncio
import time
from datetime import datetime, timezone, timedelta

# -------------------------
# Configuration
# -------------------------

TOKEN = os.getenv("TAGINA_DISCORD_TOKEN")
DATABASE_FILE = "ban_counter.db"
CONTEXT_TTL_SECONDS = 10 * 60
SUPER_BAN_COOLDOWN_HOURS = 24

if not TOKEN:
    raise RuntimeError("TAGINA_DISCORD_TOKEN environment variable is not set.")


with open("config.json", "r") as file:
    config = json.load(file)

# -------------------------
# Alias Lookup
# -------------------------   

USER_ALIASES = config["user_aliases"]
OWNER_USER_ID = config["owner_user_id"]
BLOCKED_USER_IDS = set(config["blocked_user_ids"])

# -------------------------
# Database
# -------------------------

def initialize_database():
    with sqlite3.connect(DATABASE_FILE) as connection:
        cursor = connection.cursor()

        cursor.execute("""
            SELECT sql
            FROM sqlite_master
            WHERE type = 'table'
              AND name = 'ban_requests'
        """)

        result = cursor.fetchone()

        # No table yet - create the new version
        if result is None:
            cursor.execute("""
                CREATE TABLE ban_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    target_user_id INTEGER NOT NULL,
                    requested_by_user_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    ban_value INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(message_id, target_user_id)
                )
            """)

            return

        existing_sql = result[0]

        # Old version had message_id UNIQUE by itself.
        # Migrate it so one message can contain multiple targets.
        if "message_id INTEGER NOT NULL UNIQUE" in existing_sql:
            print("Migrating database for multi-user ban requests...")

            cursor.execute("""
                CREATE TABLE ban_requests_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    target_user_id INTEGER NOT NULL,
                    requested_by_user_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    ban_value INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(message_id, target_user_id)
                )
            """)

            cursor.execute("""
                INSERT INTO ban_requests_new (
                    id,
                    guild_id,
                    target_user_id,
                    requested_by_user_id,
                    message_id,
                    created_at
                )
                SELECT
                    id,
                    guild_id,
                    target_user_id,
                    requested_by_user_id,
                    message_id,
                    created_at
                FROM ban_requests
            """)

            cursor.execute("DROP TABLE ban_requests")
            cursor.execute(
                "ALTER TABLE ban_requests_new RENAME TO ban_requests"
            )

            print("Database migration complete.")
            
        # -------------------------
        # Add ban_value if missing
        # -------------------------
        
        cursor.execute("PRAGMA table_info(ban_requests)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if "ban_value" not in columns:
            cursor.execute("""
                ALTER TABLE ban_requests
                ADD COLUMN ban_value INTEGER NOT NULL DEFAULT 1
            """)
        
            print("Added ban_value column.")


def add_ban_request(
    guild_id: int,
    target_user_id: int,
    requested_by_user_id: int,
    message_id: int,
    ban_value: int = 1
) -> bool:
    try:
        with sqlite3.connect(DATABASE_FILE) as connection:
            connection.execute("""
                INSERT INTO ban_requests (
                    guild_id,
                    target_user_id,
                    requested_by_user_id,
                    message_id,
                    ban_value
                )
                VALUES (?, ?, ?, ?, ?)
            """, (
                guild_id,
                target_user_id,
                requested_by_user_id,
                message_id,
                ban_value
            ))

        return True

    except sqlite3.IntegrityError:
        return False


def get_ban_count(guild_id: int, target_user_id: int) -> int:
    with sqlite3.connect(DATABASE_FILE) as connection:
        cursor = connection.execute("""
            SELECT COALESCE(SUM(ban_value), 0)
            FROM ban_requests
            WHERE guild_id = ?
              AND target_user_id = ?
        """, (
            guild_id,
            target_user_id
        ))

        return cursor.fetchone()[0]

def get_all_ban_counts(guild_id: int):
    with sqlite3.connect(DATABASE_FILE) as connection:
        cursor = connection.execute("""
            SELECT target_user_id, SUM(ban_value) AS ban_count
            FROM ban_requests
            WHERE guild_id = ?
            GROUP BY target_user_id
            ORDER BY ban_count DESC
        """, (guild_id,))

        return cursor.fetchall()


def get_server_stats(guild_id: int) -> dict:
    """Return aggregate stats using only existing ban metadata."""
    with sqlite3.connect(DATABASE_FILE) as connection:
        total_requests = connection.execute("""
            SELECT COUNT(DISTINCT message_id)
            FROM ban_requests
            WHERE guild_id = ?
        """, (guild_id,)).fetchone()[0]

        total_points = connection.execute("""
            SELECT COALESCE(SUM(ban_value), 0)
            FROM ban_requests
            WHERE guild_id = ?
        """, (guild_id,)).fetchone()[0]

        last_24h_requests = connection.execute("""
            SELECT COUNT(DISTINCT message_id)
            FROM ban_requests
            WHERE guild_id = ?
              AND created_at >= datetime('now', '-24 hours')
        """, (guild_id,)).fetchone()[0]

        super_bans = connection.execute("""
            SELECT COUNT(*)
            FROM ban_requests
            WHERE guild_id = ?
              AND ban_value = 10
        """, (guild_id,)).fetchone()[0]

        self_bans = connection.execute("""
            SELECT COUNT(*)
            FROM ban_requests
            WHERE guild_id = ?
              AND target_user_id = requested_by_user_id
        """, (guild_id,)).fetchone()[0]

        top_target = connection.execute("""
            SELECT target_user_id, SUM(ban_value) AS points
            FROM ban_requests
            WHERE guild_id = ?
            GROUP BY target_user_id
            ORDER BY points DESC
            LIMIT 1
        """, (guild_id,)).fetchone()

        weekly_top_target = connection.execute("""
            SELECT target_user_id, SUM(ban_value) AS points
            FROM ban_requests
            WHERE guild_id = ?
              AND created_at >= datetime('now', '-7 days')
            GROUP BY target_user_id
            ORDER BY points DESC
            LIMIT 1
        """, (guild_id,)).fetchone()

        top_requester = connection.execute("""
            SELECT requested_by_user_id, COUNT(DISTINCT message_id) AS requests
            FROM ban_requests
            WHERE guild_id = ?
            GROUP BY requested_by_user_id
            ORDER BY requests DESC
            LIMIT 1
        """, (guild_id,)).fetchone()

    return {
        "total_requests": total_requests,
        "total_points": total_points,
        "last_24h_requests": last_24h_requests,
        "super_bans": super_bans,
        "self_bans": self_bans,
        "top_target": top_target,
        "weekly_top_target": weekly_top_target,
        "top_requester": top_requester,
    }


def get_requester_request_count(
    guild_id: int,
    requester_user_id: int,
) -> int:
    with sqlite3.connect(DATABASE_FILE) as connection:
        return connection.execute("""
            SELECT COUNT(DISTINCT message_id)
            FROM ban_requests
            WHERE guild_id = ?
              AND requested_by_user_id = ?
        """, (
            guild_id,
            requester_user_id,
        )).fetchone()[0]


def get_self_ban_count(
    guild_id: int,
    user_id: int,
) -> int:
    with sqlite3.connect(DATABASE_FILE) as connection:
        return connection.execute("""
            SELECT COUNT(*)
            FROM ban_requests
            WHERE guild_id = ?
              AND target_user_id = ?
              AND requested_by_user_id = ?
        """, (
            guild_id,
            user_id,
            user_id,
        )).fetchone()[0]


def get_super_bans_received(
    guild_id: int,
    target_user_id: int,
) -> int:
    with sqlite3.connect(DATABASE_FILE) as connection:
        return connection.execute("""
            SELECT COUNT(*)
            FROM ban_requests
            WHERE guild_id = ?
              AND target_user_id = ?
              AND ban_value = 10
        """, (
            guild_id,
            target_user_id,
        )).fetchone()[0]


def get_super_ban_cooldown_remaining(
    guild_id: int,
    requester_user_id: int,
) -> int:
    """Return Super Ban cooldown seconds remaining, or 0 when ready."""
    with sqlite3.connect(DATABASE_FILE) as connection:
        row = connection.execute("""
            SELECT created_at
            FROM ban_requests
            WHERE guild_id = ?
              AND requested_by_user_id = ?
              AND ban_value = 10
            ORDER BY created_at DESC
            LIMIT 1
        """, (
            guild_id,
            requester_user_id,
        )).fetchone()

    if not row:
        return 0

    last_used = datetime.strptime(
        row[0],
        "%Y-%m-%d %H:%M:%S",
    ).replace(tzinfo=timezone.utc)

    ready_at = last_used + timedelta(hours=SUPER_BAN_COOLDOWN_HOURS)
    remaining = int(
        (ready_at - datetime.now(timezone.utc)).total_seconds()
    )

    return max(0, remaining)


def format_duration(seconds: int) -> str:
    seconds = max(0, seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes}m"

    return f"{minutes}m"


MILESTONES = {
    5: "THE PAPERWORK BEGINS",
    10: "DOUBLE DIGITS",
    25: "QUARTER-CENTURY OF COMPLAINTS",
    50: "FIFTY PIECES OF EVIDENCE",
    100: "CENTURY CLUB",
    250: "ADMINISTRATIVE NIGHTMARE",
    500: "THE LEDGER HAS A PROBLEM",
    1000: "SYSTEM LIMITS WERE A SUGGESTION",
}


def get_crossed_milestone(
    previous_count: int,
    new_count: int,
):
    crossed = [
        (threshold, label)
        for threshold, label in MILESTONES.items()
        if previous_count < threshold <= new_count
    ]

    if not crossed:
        return None

    return max(crossed, key=lambda item: item[0])



def wipe_server_data(guild_id: int) -> int:
    """
    Delete all stored ban-request data for one Discord server.
    Returns the number of rows deleted.
    """

    with sqlite3.connect(DATABASE_FILE) as connection:
        cursor = connection.execute("""
            DELETE FROM ban_requests
            WHERE guild_id = ?
        """, (guild_id,))

        return cursor.rowcount


def wipe_all_data() -> int:
    """
    Delete all ban-request data from every server.
    Returns the number of rows deleted.
    """

    with sqlite3.connect(DATABASE_FILE) as connection:
        cursor = connection.execute("""
            DELETE FROM ban_requests
        """)

        return cursor.rowcount
        
def has_used_daily_ban(guild_id: int, requester_user_id: int) -> bool:
    """
    Returns True if this user has already submitted
    a ban request in this server within the last hour.
    """

    with sqlite3.connect(DATABASE_FILE) as connection:
        cursor = connection.execute("""
            SELECT 1
            FROM ban_requests
            WHERE guild_id = ?
              AND requested_by_user_id = ?
              AND created_at >= datetime('now', '-1 hours')
            LIMIT 1
        """, (
            guild_id,
            requester_user_id
        ))

        return cursor.fetchone() is not None

# -------------------------
# Message detection
# -------------------------

def is_count_question(content: str) -> bool:
    content = content.lower()

    patterns = [
        r"\bhow many bans?\b",
        r"\bban count\b",
        r"\bban score\b",
        r"\bban total\b",
        r"\bhow many times\b.*\bban",
    ]  

    return any(re.search(pattern, content) for pattern in patterns)


def is_negative_ban_statement(content: str) -> bool:
    content = content.lower()

    patterns = [
        r"\bdon'?t\b.*\bban\b",
        r"\bdo not\b.*\bban\b",
        r"\bshouldn'?t\b.*\bban\b",
        r"\bshould not\b.*\bban\b",
        r"\bnever\b.*\bban\b",
    ]

    return any(re.search(pattern, content) for pattern in patterns)

def is_super_ban_request(content: str) -> bool:
    return bool(
        re.search(
            r"\bsuper\s+ban\b",
            content,
            re.IGNORECASE
        )
    )

def is_ban_request(content: str) -> bool:
    content = content.lower()

    patterns = [
        # can we ban CJ
        r"\bcan we\b.*\bban\b",

        # should we ban CJ
        r"\bshould we\b.*\bban\b",

        # someone ban CJ
        r"\bsomeone\b.*\bban\b",

        # mods ban CJ
        r"\bmods?\b.*\bban\b",

        # please ban CJ
        r"\bplease\b.*\bban\b",

        # ban CJ
        r"^\s*ban\b",

        # I vote we ban CJ
        r"\bvote\b.*\bban\b",

        # we need to ban CJ
        r"\bneed to\b.*\bban\b",

        # we should ban CJ
        r"\bshould\b.*\bban\b",

        # hit a fat ban on CJ
        r"\bhit\b.*\bban\b",

        # CJ deserves a ban
        r"\bdeserves?\b.*\bban\b",

        # give CJ a ban
        r"\bgive\b.*\bban\b",
        
        # super ban CJ
        r"\bsuper\s+ban\b",
    ]

    return any(re.search(pattern, content) for pattern in patterns)
    
def has_used_super_ban(
    guild_id: int,
    requester_user_id: int
) -> bool:

    with sqlite3.connect(DATABASE_FILE) as connection:
        cursor = connection.execute("""
            SELECT 1
            FROM ban_requests
            WHERE guild_id = ?
              AND requested_by_user_id = ?
              AND ban_value = 10
              AND created_at >= datetime('now', '-1 days')
            LIMIT 1
        """, (
            guild_id,
            requester_user_id
        ))

        return cursor.fetchone() is not None

def extract_ban_target(content: str):
    """
    Pull the text after the word 'ban'.

    Examples:
        can we ban Conner
        ban Conner
        should we ban Conner?
    """

    match = re.search(
        r"\bban\s+(.+?)(?:\s+please)?[?.!,]*$",
        content,
        re.IGNORECASE
    )

    if not match:
        return None

    return match.group(1).strip()


def find_member_by_name(guild: discord.Guild, name: str):
    """
    Find a member using:
        1. Alias
        2. Discord username
        3. Server display name
        4. Global display name
    """

    search = name.lower().strip()

    # Check aliases first
    if search in USER_ALIASES:
        user_id = USER_ALIASES[search]
        return guild.get_member(user_id)

    matches = []

    for member in guild.members:
        names = {
            member.name.lower(),
            member.display_name.lower(),
        }

        if member.global_name:
            names.add(member.global_name.lower())

        if search in names:
            matches.append(member)

    if len(matches) == 1:
        return matches[0]

    return None

def find_targets_in_text(
    guild: discord.Guild,
    text: str,
    requester: discord.Member = None
):
    """
    Find all known Discord users mentioned in a piece of text.

    Supports:
        - @mentions
        - USER_ALIASES
        - Discord usernames
        - display names
        - global names

    Returns users in the order they appear.
    """

    candidates = []
    

    # -------------------------
    # Self references
    # -------------------------

    if requester:
        for match in re.finditer(
            r"\b(?:me|myself)\b",
            text,
            re.IGNORECASE
        ):
            candidates.append(
                (match.start(), requester)
            )

    # -------------------------
    # Discord @mentions
    # -------------------------

    for match in re.finditer(r"<@!?(\d+)>", text):
        user_id = int(match.group(1))
        member = guild.get_member(user_id)

        if member:
            candidates.append(
                (match.start(), member)
            )

    # -------------------------
    # Known aliases
    # -------------------------

    for alias, user_id in USER_ALIASES.items():
        pattern = rf"(?<!\w){re.escape(alias)}(?!\w)"

        for match in re.finditer(
            pattern,
            text,
            re.IGNORECASE
        ):
            member = guild.get_member(user_id)

            if member:
                candidates.append(
                    (match.start(), member)
                )

    # -------------------------
    # Discord/display names
    # -------------------------

    for member in guild.members:
        possible_names = {
            member.name,
            member.display_name,
        }

        if member.global_name:
            possible_names.add(member.global_name)

        for name in possible_names:
            if not name:
                continue

            pattern = rf"(?<!\w){re.escape(name)}(?!\w)"

            for match in re.finditer(
                pattern,
                text,
                re.IGNORECASE
            ):
                candidates.append(
                    (match.start(), member)
                )

    # Sort based on where the name appeared
    candidates.sort(key=lambda item: item[0])

    # Remove duplicates while preserving order
    targets = []
    seen_ids = set()

    for _, member in candidates:
        if member.id not in seen_ids:
            targets.append(member)
            seen_ids.add(member.id)

    return targets
    
# LLM Targets
def resolve_llm_targets(
    guild: discord.Guild,
    target_names: list[str],
    requester: discord.Member
):
    targets = []
    seen_ids = set()

    for name in target_names:
        normalized = name.casefold().strip()

        # -------------------------
        # Self references
        # -------------------------

        if normalized in {
            "me",
            "myself",
            "myself please",
        }:
            member = requester
        else:
            member = find_member_by_name(
                guild,
                name
            )

        if member and member.id not in seen_ids:
            targets.append(member)
            seen_ids.add(member.id)

    return targets
    
def get_random_server_member(
    guild: discord.Guild,
    requester_id: int
):
    eligible_members = [
        member
        for member in guild.members
        if not member.bot
        and member.id != requester_id
    ]

    if not eligible_members:
        return None

    return random.choice(eligible_members)

def resolve_targets(message: discord.Message):
    content = message.content

    # -------------------------
    # Random target phrases
    # -------------------------

    if (
        is_ban_request(content)
        and re.search(
            r"\b(?:weird guy|random guy|ban roulette)\b",
            content,
            re.IGNORECASE
        )
    ):
        random_target = get_random_server_member(
            message.guild,
            message.author.id
        )

        if random_target:
            return [random_target]

    # -------------------------
    # Normal target resolution
    # -------------------------

    ban_match = re.search(
        r"\bbans?\b",
        content,
        re.IGNORECASE
    )

    if not ban_match:
        return []

   

    before_ban = content[:ban_match.start()]
    after_ban = content[ban_match.end():]

    # -------------------------
    # Some sentence structures put
    # the target BEFORE "ban"
    # -------------------------

    target_before_ban = bool(
        re.search(
            r"\bdeserves?\b.*$",
            before_ban,
            re.IGNORECASE
        )
    )

    if target_before_ban:
        targets = find_targets_in_text(
            message.guild,
            before_ban,
            message.author
        )

        if targets:
            return targets

    # -------------------------
    # Normally targets follow "ban"
    # -------------------------

    target_section = re.split(
        r"\b(?:because|since|but|although|though|while|if|when)\b",
        after_ban,
        maxsplit=1,
        flags=re.IGNORECASE
    )[0]

    targets = find_targets_in_text(
        message.guild,
        target_section,
        message.author
    )

    if targets:
        return targets

    # -------------------------
    # Last fallback:
    # look before "ban"
    # -------------------------

    return find_targets_in_text(
        message.guild,
        before_ban,
        message.author
    )
    
def get_ban_title(count: int):
    if count >= 1000:
        return "🗄️ BEYOND ADMINISTRATIVE CONTROL"
    elif count >= 500:
        return "💀 FINAL BOSS OF BAD DECISIONS"
    elif count >= 250:
        return "☢️ COMMUNITY HAZARD"
    elif count >= 150:
        return "🔥 SERVER-WIDE LIABILITY"
    elif count >= 100:
        return "📢 PUBLIC ENEMY"
    elif count >= 75:
        return "🧾 PERMANENTLY ON THE LIST"
    elif count >= 50:
        return "🚨 REPEAT PUBLIC NUISANCE"
    elif count >= 35:
        return "🧨 SERIAL MENACE"
    elif count >= 25:
        return "😈 CERTIFIED PROBLEM"
    elif count >= 15:
        return "👁️ UNDER SUSPICIOUS OBSERVATION"
    elif count >= 10:
        return "👀 PERSON OF INTEREST"
    elif count >= 5:
        return "🤨 REPEAT OFFENDER"

    return None
    
def has_ai_trigger(content: str) -> bool:
    return bool(
        re.search(
            r"\b(?:ban bot|tagina)\b",
            content,
            re.IGNORECASE
        )
    )


def remove_ai_trigger(content: str) -> str:
    return re.sub(
        r"\b(?:ban bot|tagina)\b[,:]?\s*",
        "",
        content,
        count=1,
        flags=re.IGNORECASE
    ).strip()


# -------------------------
# Short-lived conversation context
# -------------------------

# RAM only: no raw Discord message text is stored here.
conversation_context = {}


def get_recent_context(
    guild_id: int,
    user_id: int,
):
    key = (guild_id, user_id)
    entry = conversation_context.get(key)

    if not entry:
        return None

    if time.monotonic() - entry["saved_at"] > CONTEXT_TTL_SECONDS:
        conversation_context.pop(key, None)
        return None

    return {
        "action": entry["action"],
        "targets": entry["targets"],
        "super_ban": entry["super_ban"],
    }


def save_recent_context(
    guild_id: int,
    user_id: int,
    intent,
):
    conversation_context[(guild_id, user_id)] = {
        "action": intent.action,
        "targets": list(intent.targets),
        "super_ban": intent.super_ban,
        "saved_at": time.monotonic(),
    }


# -------------------------
# Discord
# -------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)

llm_semaphore = asyncio.Semaphore(1)


async def get_tagina_flavor(
    message: discord.Message,
    event: str,
    target_name: str = "",
    details: dict | None = None,
    fallback: str = "",
) -> str:
    try:
        async with message.channel.typing():
            async with llm_semaphore:
                flavor = await asyncio.to_thread(
                    generate_tagina_response,
                    event,
                    message.author.display_name,
                    target_name,
                    details,
                )

        return flavor or fallback

    except Exception as error:
        print(f"TAGINA personality generation failed: {error}")
        return fallback


async def reply_with_tagina(
    message: discord.Message,
    event: str,
    target_name: str = "",
    details: dict | None = None,
    facts: str = "",
    fallback: str = "",
):
    flavor = await get_tagina_flavor(
        message,
        event,
        target_name=target_name,
        details=details,
        fallback=fallback,
    )

    response = flavor
    if facts:
        response += f"\n\n{facts}"

    await message.reply(
        response,
        allowed_mentions=discord.AllowedMentions.none(),
    )



@client.event
async def on_ready():
    print(f"Logged in as {client.user}")
    print("Ban Counter is ready.")


@client.event
async def on_message(message: discord.Message):

    # Ignore bots
    if message.author.bot:
        return

    # Ignore DMs
    if message.guild is None:
        return

    content = message.content
    # -------------------------
    # AI interpretation
    # -------------------------
    
    ai_intent = None
    
    if has_ai_trigger(content):
        llm_content = remove_ai_trigger(content)
        recent_context = get_recent_context(
            message.guild.id,
            message.author.id,
        )

        async with message.channel.typing():
            async with llm_semaphore:
                ai_intent = await asyncio.to_thread(
                    interpret_message,
                    llm_content,
                    recent_context,
                )

        print(f"LLM intent: {ai_intent}")

        if ai_intent.action != "none":
            save_recent_context(
                message.guild.id,
                message.author.id,
                ai_intent,
            )
        else:
            return

    # -------------------------
    # Owner-only data wipe
    # -------------------------

    if message.author.id == OWNER_USER_ID:

        if content.strip().lower() == "!wipebanstats confirm":
            deleted = wipe_server_data(message.guild.id)

            await message.reply(
                f"🧹 Wiped **{deleted}** ban records for this server."
            )
            return

        if content.strip().lower() == "!wipebanstats all confirm":
            deleted = wipe_all_data()

            await message.reply(
                f"☢️ Wiped **{deleted}** ban records from ALL servers."
            )
            return
            

    # -------------------------
    # Super Ban cooldown status
    # -------------------------

    if (
        (ai_intent and ai_intent.action == "cooldown")
        or content.strip().lower() in {
            "super ban cooldown",
            "super ban ready",
        }
    ):
        remaining = get_super_ban_cooldown_remaining(
            message.guild.id,
            message.author.id,
        )

        if remaining <= 0:
            await reply_with_tagina(
                message,
                event="cooldown_ready",
                details={"status": "ready"},
                facts="☢️ **SUPER BAN: READY**",
                fallback="THE BUTTON IS LIVE. TRY TO USE IT RESPONSIBLY. OR DON'T.",
            )
        else:
            await reply_with_tagina(
                message,
                event="cooldown_wait",
                details={"status": "recharging"},
                facts=(
                    "⏳ **SUPER BAN RECHARGE:** "
                    f"approximately **{format_duration(remaining)}**"
                ),
                fallback="NOPE. THE BIG RED BUTTON IS STILL RECHARGING.",
            )

        return

    # -------------------------
    # Server ban statistics
    # -------------------------

    if (
        (ai_intent and ai_intent.action == "stats")
        or content.strip().lower() in {
            "ban stats",
            "server ban stats",
            "ban report",
        }
    ):
        stats = get_server_stats(message.guild.id)

        if stats["total_requests"] == 0:
            await reply_with_tagina(
                message,
                event="empty_stats",
                facts="📊 **No ban activity has been recorded yet.**",
                fallback="THE LEDGER IS EMPTY. SOMEHOW YOU PEOPLE HAVE BEHAVED.",
            )
            return

        def display_name(user_id: int) -> str:
            member = message.guild.get_member(user_id)
            return member.display_name if member else f"Unknown User ({user_id})"

        top_target = stats["top_target"]
        weekly_top = stats["weekly_top_target"]
        top_requester = stats["top_requester"]

        stat_lines = [
            "📊 **TAGINA BAN REPORT**",
            f"Requests recorded: **{stats['total_requests']}**",
            f"Total ban points: **{stats['total_points']}**",
            f"Requests in last 24h: **{stats['last_24h_requests']}**",
            f"Super Bans deployed: **{stats['super_bans']}**",
            f"Self-bans: **{stats['self_bans']}**",
        ]

        if top_target:
            stat_lines.append(
                "Most banned overall: "
                f"**{display_name(top_target[0])}** — **{top_target[1]} points**"
            )

        if weekly_top:
            stat_lines.append(
                "Most banned this week: "
                f"**{display_name(weekly_top[0])}** — **{weekly_top[1]} points**"
            )

        if top_requester:
            stat_lines.append(
                "Most prolific accuser: "
                f"**{display_name(top_requester[0])}** — "
                f"**{top_requester[1]} requests**"
            )

        flavor = await get_tagina_flavor(
            message,
            event="server_stats",
            details={
                "has_activity": True,
                "includes_weekly_leader": weekly_top is not None,
            },
            fallback="I RAN THE NUMBERS. THE NUMBERS ARE EMBARRASSING.",
        )

        await message.reply(
            flavor + "\n\n" + "\n".join(stat_lines),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    # -------------------------
    # Ban leaderboard
    # -------------------------

    if (
        (ai_intent and ai_intent.action == "leaderboard")
        or content.strip().lower() in {
            "all bans",
            "ban leaderboard",
            "show all bans",
        }
    ):
        results = get_all_ban_counts(message.guild.id)

        if not results:
            await reply_with_tagina(
                message,
                event="empty_leaderboard",
                facts="🔨 **No ban requests have been recorded yet.**",
                fallback="THE LEADERBOARD IS EMPTY. DISAPPOINTING.",
            )
            return

        lines = []

        for index, (user_id, count) in enumerate(results, start=1):
            member = message.guild.get_member(user_id)

            if member:
                name = member.display_name
            else:
                name = f"Unknown User ({user_id})"

            lines.append(
                f"{index}. **{name}** — {count}"
            )

        await message.reply(
            "🔨 **Ban Leaderboard**\n\n"
            + "\n".join(lines)
        )

        return
    # -------------------------
    # Asking for ban count
    # -------------------------

    if (
        (ai_intent and ai_intent.action == "count")
        or is_count_question(content)
    ):
    
        if ai_intent and ai_intent.action == "count":
            targets = resolve_llm_targets(
                message.guild,
                ai_intent.targets,
                message.author
            )
        else:
            targets = resolve_targets(message)

        if not targets:
            await reply_with_tagina(
                message,
                event="unknown_count_target",
                details={"request_type": "count"},
                facts=(
                    "I couldn't resolve that person. "
                    "Try an @mention or a known name."
                ),
                fallback="WHO? GIVE ME A NAME THAT EXISTS.",
            )
            return

        # One target
        if len(targets) == 1:
            target = targets[0]

            count = get_ban_count(
                message.guild.id,
                target.id
            )

            await message.reply(
                f"🔨 **{target.display_name}** has received "
                f"**{count} ban request{'s' if count != 1 else ''}.**"
            )

            return

        # Multiple targets
        lines = []

        for target in targets:
            count = get_ban_count(
                message.guild.id,
                target.id
            )

            lines.append(
                f"🔨 **{target.display_name}** — {count}"
            )

        await message.reply(
            "Current ban counts:\n\n"
            + "\n".join(lines)
        )

        return

    # -------------------------
    # Ignore negative statements
    # -------------------------

    if is_negative_ban_statement(content):
        return

    # -------------------------
    # Detect ban request
    # -------------------------
    
    if ai_intent and ai_intent.action == "ban":
        targets = resolve_llm_targets(
            message.guild,
            ai_intent.targets,
            message.author
        )
    
        super_ban = ai_intent.super_ban
    
    else:
        if not is_ban_request(content):
            return
    
        targets = resolve_targets(message)
    
        super_ban = is_super_ban_request(content)
    
    
    if not targets:
        await reply_with_tagina(
            message,
            event="unknown_ban_target",
            details={"request_type": "ban"},
            facts=(
                "I couldn't resolve the ban target. "
                "Try an @mention or a known name."
            ),
            fallback="WHO THE HELL IS THAT. GIVE ME A REAL TARGET.",
        )
        return
    
    # -------------------------
    # Ban type
    # -------------------------
    
    ban_value = 1
    
    if super_ban:
        if has_used_super_ban(
            message.guild.id,
            message.author.id
        ):
            remaining = get_super_ban_cooldown_remaining(
                message.guild.id,
                message.author.id,
            )
            await reply_with_tagina(
                message,
                event="super_ban_blocked",
                details={"status": "recharging"},
                facts=(
                    "🚫 **SUPER BAN RECHARGE:** "
                    f"approximately **{format_duration(remaining)}**"
                ),
                fallback="NOT YET. THE BIG RED BUTTON IS STILL RECHARGING.",
            )
            return
    
        targets = targets[:1]
        ban_value = 10
        
    # -------------------------
    # Limited users:
    # one ban every hour
    # with a 50% backfire chance
    # -------------------------

    backfired = False

    if message.author.id in BLOCKED_USER_IDS:

        if has_used_daily_ban(
            message.guild.id,
            message.author.id
        ):
            # Silently ignore additional attempts
            return

        # Limited users can only target one person
        targets = targets[:1]

        # 50/50 chance their ban hits themselves instead
        if random.random() < 0.5:
            targets = [message.author]
            backfired = True

    # -------------------------
    # Record each target
    # -------------------------

    recorded_targets = []
    attempted_bot_target = False

    for target in targets:

        # Don't let people request the bot itself
        if target.id == client.user.id:
            attempted_bot_target = True
            continue

        added = add_ban_request(
            guild_id=message.guild.id,
            target_user_id=target.id,
            requested_by_user_id=message.author.id,
            message_id=message.id,
            ban_value=ban_value
        )
        if added:
            recorded_targets.append(target)

    if not recorded_targets:
        if attempted_bot_target:
            await reply_with_tagina(
                message,
                event="bot_target",
                target_name=client.user.display_name,
                facts="🤖 **TAGINA cannot receive ban points.**",
                fallback="NICE TRY. I AM THE PAPERWORK.",
            )
        return
       

    # -------------------------
    # Respond
    # -------------------------
    
    if len(recorded_targets) == 1:
        target = recorded_targets[0]
    
        count = get_ban_count(
            message.guild.id,
            target.id
        )
    
        title = get_ban_title(count)
    
        # -------------------------
        # Determine what happened
        # -------------------------
    
        if super_ban and backfired:
            event = "super_ban_backfire"            fallback = (
                "☢️ SUPER BAN CATASTROPHIC BACKFIRE. "
                "BEAUTIFUL WORK, TIMMY."
            )
    
        elif super_ban:
            event = "super_ban"
            fallback = (
                "🚨 SUPER BAN DEPLOYED. "
                "FACTORY HAS SPOKEN."
            )
    
        elif backfired:
            event = "ban_backfire"
            fallback = (
                "💥 BAN BACKFIRE. "
                "YOU MANAGED TO SHOOT YOURSELF, TIMMY."
            )
    
        else:
            event = "normal_ban"
            fallback = (
                "🔨 BAN REQUEST RECORDED. "
                "ANOTHER NAME FOR THE FACTORY LEDGER."
            )
    
        # -------------------------
        # Let TAGINA react
        # -------------------------
    

        flavor = await get_tagina_flavor(
            message,
            event,
            target_name=target.display_name,
            details={
                "super_ban": super_ban,
                "backfired": backfired,
            },
            fallback=fallback,
        )

        # Deterministic facts
        # -------------------------
    

    

        

            f"\n\n🔨 **{target.display_name}** — "
            f"**{count} BAN {point_word}**"
        )
    

            response += (
                f"\n💥 THIS HIT: **{ban_value} POINTS**"
            )
    

            response += (
                f"\nDESIGNATION: **{title}**"
            )
    

        # -------------------------
        # Milestones / achievements
        # -------------------------

        previous_count = max(0, count - ban_value)
        milestone = get_crossed_milestone(
            previous_count,
            count,
        )

        if milestone:
            threshold, label = milestone
            response += (
                f"\n🏆 MILESTONE {threshold}: **{label}**"
            )

        requester_total = get_requester_request_count(
            message.guild.id,
            message.author.id,
        )

        if requester_total == 25:
            response += (
                "\n📋 ACHIEVEMENT: **FREQUENT FILER**"
            )
        elif requester_total == 100:
            response += (
                "\n🗂️ ACHIEVEMENT: **BAN INDUSTRIALIST**"
            )

        if target.id == message.author.id:
            self_ban_total = get_self_ban_count(
                message.guild.id,
                target.id,
            )

            if self_ban_total == 5:
                response += (
                    "\n🪞 ACHIEVEMENT: **SELF REPORTER**"
                )
            elif self_ban_total == 10:
                response += (
                    "\n🪞 ACHIEVEMENT: **OWN WORST ENEMY**"
                )

        if super_ban:
            super_bans_received = get_super_bans_received(
                message.guild.id,
                target.id,
            )

            if super_bans_received == 3:
                response += (
                    "\n☢️ ACHIEVEMENT: **SUPER BAN MAGNET**"
                )

            response,
            allowed_mentions=discord.AllowedMentions.none()
        )
    


    lines = []

    for target in recorded_targets:
        count = get_ban_count(
            message.guild.id,
            target.id
        )

        title = get_ban_title(count)

        line = f"🔨 **{target.display_name}** — {count}"

        if title:
            line += f" — **{title}**"

        lines.append(line)

    await message.reply(
        "Multiple ban requests recorded:\n\n"
        + "\n".join(lines)
    )


# -------------------------
# Start
# -------------------------

initialize_database()
client.run(TOKEN)
