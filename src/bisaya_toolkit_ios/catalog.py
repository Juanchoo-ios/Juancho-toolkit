from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass


USER_AGENT = "Bisaya-Toolkit-iOS/1.1"
CATALOGS = {
    "Skins": "https://raw.githubusercontent.com/missnapokita/masterkiter/refs/heads/main/skins.json",
    "Suggested": "https://raw.githubusercontent.com/missnapokita/masterkiter/refs/heads/main/suggested.json",
    "Custom Skins": "https://raw.githubusercontent.com/missnapokita/masterkiter/refs/heads/main/custom.json",
    "Meme Skins": "https://raw.githubusercontent.com/missnapokita/masterkiter/refs/heads/main/meme.json",
}


@dataclass(frozen=True)
class SkinItem:
    name: str
    hero: str
    image_url: str
    zip_url: str
    catalog: str


def _first(data: dict, keys: tuple[str, ...]) -> str:
    for key in keys:
        value = data.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _walk(value):
    if isinstance(value, list):
        for item in value:
            yield from _walk(item)
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _walk(child)


def load_catalogs(timeout: int = 45) -> list[SkinItem]:
    unique: dict[tuple[str, str, str], SkinItem] = {}
    for catalog, url in CATALOGS.items():
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(20 * 1024 * 1024 + 1))
        for entry in _walk(payload):
            zip_url = _first(entry, ("script", "download", "download_url", "zip", "zip_url", "file"))
            name = _first(entry, ("skin_name", "name", "title", "item_name", "label"))
            if not name or not zip_url.startswith(("https://", "http://")):
                continue
            hero = _first(entry, ("hero_name", "hero", "character", "heroName"))
            image = _first(entry, ("image", "image_url", "landscape", "portrait", "head", "icon", "thumbnail"))
            key = (hero.casefold(), name.casefold(), zip_url)
            unique.setdefault(key, SkinItem(name, hero, image, zip_url, catalog))
    return sorted(unique.values(), key=lambda item: (item.hero.casefold(), item.name.casefold()))
