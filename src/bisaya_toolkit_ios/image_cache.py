from __future__ import annotations

import hashlib
import threading
import urllib.request
from collections import OrderedDict
from pathlib import Path

from PIL import Image

from .catalog import USER_AGENT


MAX_IMAGE_BYTES = 12 * 1024 * 1024


class ThumbnailCache:
    """Disk + memory LRU cache that downloads every image URL only once."""

    def __init__(self, memory_limit: int = 96):
        self.folder = Path.home() / "Library" / "Caches" / "BisayaToolkit" / "thumbnails"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.memory_limit = memory_limit
        self.memory: OrderedDict[str, Path] = OrderedDict()
        self.locks: dict[str, threading.Lock] = {}
        self.guard = threading.Lock()

    def get(self, url: str) -> Path | None:
        if not url.startswith(("https://", "http://")):
            return None
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()
        target = self.folder / f"{key}.png"
        with self.guard:
            lock = self.locks.setdefault(key, threading.Lock())
        with lock:
            if not target.is_file():
                self._download_thumbnail(url, target)
        with self.guard:
            self.memory[key] = target
            self.memory.move_to_end(key)
            while len(self.memory) > self.memory_limit:
                self.memory.popitem(last=False)
        return target

    def _download_thumbnail(self, url: str, target: Path) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=45) as response:
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > MAX_IMAGE_BYTES:
                raise ValueError("Image exceeds the cache safety limit.")
            data = response.read(MAX_IMAGE_BYTES + 1)
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError("Image exceeds the cache safety limit.")
        temporary = target.with_suffix(".tmp")
        from io import BytesIO
        with Image.open(BytesIO(data)) as source:
            source = source.convert("RGB")
            source.thumbnail((360, 360), Image.Resampling.LANCZOS)
            source.save(temporary, format="PNG", optimize=True)
        temporary.replace(target)
