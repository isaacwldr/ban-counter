import sys

from llm_parser import MessageIntent, interpret_message


TESTS = [
    (
        "ban Conner",
        None,
        MessageIntent("ban", ["Conner"], False),
    ),
    (
        "Grok remove this vile creature Conner from my sight",
        None,
        MessageIntent("ban", ["Conner"], False),
    ),
    (
        "I invoke the ancient rite of the super ban upon CJ",
        None,
        MessageIntent("ban", ["CJ"], True),
    ),
    (
        "how many bans does Tyler have",
        None,
        MessageIntent("count", ["Tyler"], False),
    ),
    (
        "How persecuted is Tyler these days?",
        None,
        MessageIntent("count", ["Tyler"], False),
    ),
    (
        "don't ban Ed",
        None,
        MessageIntent("none", [], False),
    ),
    (
        "we should not ban CJ",
        None,
        MessageIntent("none", [], False),
    ),
    (
        "show me the ban leaderboard",
        None,
        MessageIntent("leaderboard", [], False),
    ),
    (
        "CJ has forfeited his right to remain among us",
        None,
        MessageIntent("ban", ["CJ"], False),
    ),
    (
        "what are we playing tonight?",
        None,
        MessageIntent("none", [], False),
    ),
    (
        "ban bot cj ban please",
        None,
        MessageIntent("ban", ["cj"], False),
    ),
    (
        "tagina I require a ban on ed",
        None,
        MessageIntent("ban", ["ed"], False),
    ),
    (
        "Tagina can I super ban yet?",
        None,
        MessageIntent("cooldown", [], False),
    ),
    (
        "Tagina how long until my super ban comes back?",
        None,
        MessageIntent("cooldown", [], False),
    ),
    (
        "Tagina give me the server ban stats",
        None,
        MessageIntent("stats", [], False),
    ),
    (
        "Tagina what about CJ?",
        {
            "action": "count",
            "targets": ["Tyler"],
            "super_ban": False,
        },
        MessageIntent("count", ["CJ"], False),
    ),
    (
        "Tagina what about him?",
        {
            "action": "count",
            "targets": ["Tyler"],
            "super_ban": False,
        },
        MessageIntent("count", ["Tyler"], False),
    ),
]


def normalized(intent: MessageIntent):
    return (
        intent.action,
        [target.casefold() for target in intent.targets],
        intent.super_ban,
    )


failures = []

for message, context, expected in TESTS:
    result = interpret_message(message, context=context)
    passed = normalized(result) == normalized(expected)

    print(f"\n{'PASS' if passed else 'FAIL'}: {message}")
    print(f"RESULT:   {result}")
    print(f"EXPECTED: {expected}")

    if not passed:
        failures.append((message, result, expected))


print(f"\n{len(TESTS) - len(failures)}/{len(TESTS)} tests passed.")

if failures:
    sys.exit(1)
