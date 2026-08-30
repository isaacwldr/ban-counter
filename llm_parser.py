from dataclasses import dataclass, field


@dataclass
class MessageIntent:
    action: str = "none"
    targets: list[str] = field(default_factory=list)
    super_ban: bool = False