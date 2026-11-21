from dataclasses import dataclass


@dataclass(frozen=True)
class Media:
    data: bytes
    mime_type: str
