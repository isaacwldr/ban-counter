import os
import re
import sqlite3
import discord
import json

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
                    message_content TEXT,
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
                    message_content TEXT,
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
                    message_content,
                    created_at
                )
                SELECT
                    id,
                    guild_id,
                    target_user_id,
                    requested_by_user_id,
                    message_id,
                    message_content,
                    created_at
                FROM ban_requests
            """)

            cursor.execute("DROP TABLE ban_requests")
            cursor.execute(
                "ALTER TABLE ban_requests_new RENAME TO ban_requests"
            )

            print("Database migration complete.")


def add_ban_request(
    guild_id: int,
    target_user_id: int,
    requested_by_user_id: int,
    message_id: int
) -> bool:
    try:
        with sqlite3.connect(DATABASE_FILE) as connection:
            connection.execute("""
                INSERT INTO ban_requests (
                    guild_id,
                    target_user_id,
                    requested_by_user_id,
                    message_id
                )
                VALUES (?, ?, ?, ?)
            """, (
                guild_id,
                target_user_id,
                requested_by_user_id,
                message_id
            ))

        return True

    except sqlite3.IntegrityError:
        return False


def get_ban_count(guild_id: int, target_user_id: int) -> int:
    with sqlite3.connect(DATABASE_FILE) as connection:
        cursor = connection.execute("""
            SELECT COUNT(*)
            FROM ban_requests
            WHERE guild_id = ?
              AND target_user_id = ?
        """, (guild_id, target_user_id))

        return cursor.fetchone()[0]


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

# -------------------------
# Message detection
# -------------------------

def is_count_question(content: str) -> bool:
    content = content.lower()

    patterns = [
        r"how many .*ban",
        r"ban count",
        r"ban score",
        r"ban total",
        r"how many times .*ban",
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
    ]

    return any(re.search(pattern, content) for pattern in patterns)

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

def find_targets_in_text(guild: discord.Guild, text: str):
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

def resolve_targets(message: discord.Message):
    """
    Resolve one or more people associated with the ban request.

    Usually looks after the word "ban".

    Examples:
        can we ban CJ
        can we ban CJ and the mighty warrior
        hit a fat ban on CJ and Ed

    For phrases like:
        CJ deserves a ban

    it looks before the word "ban" instead.
    """

    content = message.content

    ban_match = re.search(
        r"\bban\b",
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
            before_ban
        )

        if targets:
            return targets

    # -------------------------
    # Normally the targets follow "ban"
    # -------------------------

    # Stop when the sentence turns into an explanation:
    #
    # can we ban CJ because @Ed keeps...
    #
    # This prevents Ed from becoming another target.

    target_section = re.split(
        r"\b(?:because|since|but|although|though|while|if|when)\b",
        after_ban,
        maxsplit=1,
        flags=re.IGNORECASE
    )[0]

    targets = find_targets_in_text(
        message.guild,
        target_section
    )

    if targets:
        return targets

    # -------------------------
    # Last fallback:
    # look immediately before ban
    # -------------------------

    return find_targets_in_text(
        message.guild,
        before_ban
    )

# -------------------------
# Discord
# -------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

client = discord.Client(intents=intents)


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
    # Asking for ban count
    # -------------------------

    if is_count_question(content):

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
    # Ignore blocked users
    # -------------------------

    if message.author.id in BLOCKED_USER_IDS:
        return

    # -------------------------
    # Detect ban request
    # -------------------------

    if not is_ban_request(content):
        return

    targets = resolve_targets(message)

    if not targets:
        await message.reply(
            "I couldn't figure out who we're banning. "
            "Try using an @mention or a known name."
        )
        return

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
            message_id=message.id
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

        await message.reply(
            f"🔨 Ban request recorded for **{target.display_name}**.\n"
            f"They are now at **{count}**."
        )

        return

    lines = []

    for target in recorded_targets:
        count = get_ban_count(
            message.guild.id,
            target.id
        )

        lines.append(
            f"🔨 **{target.display_name}** — {count}"
        )

    await message.reply(
        "Multiple ban requests recorded:\n\n"
        + "\n".join(lines)
    )


# -------------------------
# Start
# -------------------------

initialize_database()
client.run(TOKEN)