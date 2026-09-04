from __future__ import annotations

import gc
import os
import shutil
import tempfile
import threading
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

from .catalog import SkinItem, USER_AGENT
from .converter_engine.mlbb_ios_converter import Converter, ConvertOptions


MAX_ARCHIVE = 2 * 1024 * 1024 * 1024
MAX_EXTRACTED = 8 * 1024 * 1024 * 1024
MAX_FILES = 50_000
Progress = Callable[[float, str], None]


def filza_output_folder() -> Path:
    # On iOS Path.home() is the application container. Filza exposes Documents.
    result = Path.home() / "Documents" / "Bisaya Toolkit"
    result.mkdir(parents=True, exist_ok=True)
    return result


def safe_name(value: str) -> str:
    cleaned = "".join("_" if char in '<>:"/\\|?*' else char for char in value).strip(" .")
    return cleaned[:110] or "Converted Skin"


def unique_zip(folder: Path, base: str) -> Path:
    candidate = folder / f"{base}_iOS.zip"
    number = 2
    while candidate.exists():
        candidate = folder / f"{base}_iOS_{number}.zip"
        number += 1
    return candidate


def download(url: str, target: Path, cancelled: threading.Event, progress: Progress) -> None:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/zip,application/octet-stream,*/*",
        "Accept-Encoding": "identity",
    })
    with urllib.request.urlopen(request, timeout=180) as response, target.open("wb") as output:
        total = int(response.headers.get("Content-Length") or 0)
        if total > MAX_ARCHIVE:
            raise ValueError("Download is larger than the 2 GB safety limit.")
        received = 0
        while True:
            if cancelled.is_set():
                raise InterruptedError("Cancelled")
            block = response.read(512 * 1024)
            if not block:
                break
            received += len(block)
            if received > MAX_ARCHIVE:
                raise ValueError("Download exceeded the 2 GB safety limit.")
            output.write(block)
            progress(0.55 * received / total if total else 0.05, f"Downloading {received / 1048576:.1f} MB")


def safe_extract(archive: Path, destination: Path, cancelled: threading.Event, progress: Progress) -> int:
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if len(members) > MAX_FILES:
            raise ValueError("ZIP contains too many files.")
        total_size = sum(member.file_size for member in members)
        if total_size > MAX_EXTRACTED:
            raise ValueError("Extracted package is larger than the 8 GB safety limit.")
        base = destination.resolve()
        written = 0
        for index, member in enumerate(members, 1):
            if cancelled.is_set():
                raise InterruptedError("Cancelled")
            relative = PurePosixPath(member.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError(f"Unsafe ZIP path: {member.filename}")
            target = destination.joinpath(*relative.parts)
            resolved = target.resolve()
            if resolved != base and base not in resolved.parents:
                raise ValueError(f"Unsafe ZIP path: {member.filename}")
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as input_file, target.open("wb") as output_file:
                    shutil.copyfileobj(input_file, output_file, 1024 * 1024)
                written += 1
            progress(0.55 + 0.12 * index / max(1, len(members)), f"Extracting {index}/{len(members)}")
    return written


def process_skin(item: SkinItem, cancelled: threading.Event, progress: Progress) -> tuple[Path, object, int]:
    output = filza_output_folder()
    temporary_parent = Path(tempfile.mkdtemp(prefix="btk_ios_"))
    try:
        archive = temporary_parent / "download.zip"
        extracted = temporary_parent / safe_name(f"{item.hero} - {item.name}")
        converted_parent = temporary_parent / "converted"
        extracted.mkdir(); converted_parent.mkdir()
        download(item.zip_url, archive, cancelled, progress)
        if not zipfile.is_zipfile(archive):
            raise ValueError("The server did not return a valid ZIP.")
        count = safe_extract(archive, extracted, cancelled, progress)
        if not any(extracted.rglob("*.unity3d")):
            raise ValueError("The package contains no .unity3d files.")

        shader_folder = Path(__file__).parent / "converter_engine" / "shaders"
        converter = Converter(
            extracted,
            shader_folder,
            converted_parent,
            ConvertOptions(create_zip=True, full_validation=True, strict_matching=True, preserve_shadows=True),
            logger=lambda message: progress(0.69, message),
            progress=lambda value, message: progress(0.68 + value * 0.30, message),
        )
        _folder, result_zip, _report = converter.convert()
        if result_zip is None or not result_zip.is_file():
            raise RuntimeError("Converter did not create a final ZIP.")
        with zipfile.ZipFile(result_zip) as checked:
            bad = checked.testzip()
            if bad:
                raise RuntimeError(f"Final ZIP verification failed: {bad}")
        final_path = unique_zip(output, safe_name(f"{item.hero} - {item.name}"))
        shutil.move(result_zip, final_path)
        stats = converter.stats
        converter.jobs.clear(); converter.catalog = None
        del converter
        gc.collect()
        progress(1.0, "Complete")
        return final_path, stats, count
    finally:
        shutil.rmtree(temporary_parent, ignore_errors=True)
