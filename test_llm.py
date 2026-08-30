from llm_parser import interpret_message


tests = [
    "ban Conner",
    "Grok remove this vile creature Conner from my sight",
    "I invoke the ancient rite of the super ban upon CJ",
    "how many bans does Tyler have",
    "How persecuted is Tyler these days?",
    "don't ban Ed",
    "we should not ban CJ",
    "show me the ban leaderboard",
    "CJ has forfeited his right to remain among us",
    "what are we playing tonight?",
    "ban bot cj ban please",
    "tagina I require a ban on ed",
    "Ban Bot, remove Conner from this mortal realm",
    "Tagina I require a ban on Ed",
    "Tagina invoke a super ban upon CJ",
    "Tagina don't ban Tyler",
]


for message in tests:
    result = interpret_message(message)

    print(f"\nMESSAGE: {message}")
    print(f"RESULT:  {result}")