from llm_parser import interpret_message
from personality import generate_tagina_response

import os
import re
import sqlite3
import discord
import json
import random
import asyncio

# -------------------------
# Configuration
# -------------------------

TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_FILE = "ban_counter.db"

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable is not set.")


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
    a ban request in this server within the last 24 hours.
    """

    with sqlite3.connect(DATABASE_FILE) as connection:
        cursor = connection.execute("""
            SELECT 1
            FROM ban_requests
            WHERE guild_id = ?
              AND requested_by_user_id = ?
              AND created_at >= datetime('now', '-24 hours')
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
    if count >= 100:
        return "☢️ EXISTENTIAL THREAT"
    elif count >= 50:
        return "🚨 ENEMY OF THE SERVER"
    elif count >= 25:
        return "⚠️ PUBLIC MENACE"
    elif count >= 10:
        return "👀 PERSON OF INTEREST"
    elif count >= 5:
        return "🤨 SUSPICIOUS INDIVIDUAL"

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
# Discord
# -------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)

llm_semaphore = asyncio.Semaphore(1)


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
    
        async with message.channel.typing():
            async with llm_semaphore:
                ai_intent = await asyncio.to_thread(
                    interpret_message,
                    llm_content
                )
    
        print(f"LLM intent: {ai_intent}")
    
        if ai_intent.action == "none":
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
            await message.reply("No ban requests have been recorded yet.")
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
            await message.reply(
                "I couldn't figure out who you're asking about. "
                "Try using an @mention or a known name."
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
        await message.reply(
            "I couldn't figure out who we're banning. "
            "Try using an @mention or a known name."
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
            await message.reply(
                "🚫 Your Super Ban is still recharging."
            )
            return
    
        targets = targets[:1]
        ban_value = 10
        
    # -------------------------
    # Limited users:
    # one ban every 24 hours
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

    for target in targets:

        # Don't let people request the bot itself
        if target.id == client.user.id:
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
            event = "super_ban_backfire"
            fallback = (
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
    
        try:
            async with message.channel.typing():
                async with llm_semaphore:
                    flavor = await asyncio.to_thread(
                        generate_tagina_response,
                        event,
                        message.author.display_name,
                        target.display_name
                    )
    
            if not flavor:
                flavor = fallback
    
        except Exception as error:
            print(
                f"TAGINA personality generation failed: {error}"
            )
            flavor = fallback
    
        # -------------------------
        # Deterministic facts
        # -------------------------
    
        response = flavor
    
        point_word = "POINT" if count == 1 else "POINTS"
        
        response += (
            f"\n\n🔨 **{target.display_name}** — "
            f"**{count} BAN {point_word}**"
        )
    
        if super_ban:
            response += (
                f"\n💥 THIS HIT: **{ban_value} POINTS**"
            )
    
        if title:
            response += (
                f"\nDESIGNATION: **{title}**"
            )
    
        await message.reply(
            response,
            allowed_mentions=discord.AllowedMentions.none()
        )
    
        return

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