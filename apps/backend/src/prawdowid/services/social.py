"""Social media links: their posts cannot be read from a link without logging in."""

from urllib.parse import urlsplit

SOCIAL_PLATFORMS = {
    "x.com": "X",
    "twitter.com": "X",
    "t.co": "X",
    "tiktok.com": "TikTok",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "fb.com": "Facebook",
    "fb.watch": "Facebook",
}


def social_platform(url: str) -> str | None:
    """Platform name when ``url`` points at a social network (any subdomain)."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    for domain, name in SOCIAL_PLATFORMS.items():
        if host == domain or host.endswith("." + domain):
            return name
    return None


def social_link_message(platform: str) -> str:
    return (
        f"Nie mogę odczytać wpisu z serwisu {platform} z samego linku. "
        "Zrób zrzut ekranu wpisu i wrzuć go jako Zdjęcie, a nagranie pobierz i wrzuć jako Wideo."
    )
