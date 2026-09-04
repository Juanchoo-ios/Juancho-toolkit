from __future__ import annotations

import argparse
import copy
import gc
import json
import os
import queue
import re
import shutil
import stat
import sys
import tempfile
import threading
import traceback
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable

import UnityPy
from UnityPy.classes.math import ColorRGBA
from UnityPy.enums import TextureFormat


APP_NAME = "MLBB iOS Auto Converter"
APP_VERSION = "1.0.4"
IOS_BUILD_TARGET = 9
METAL_SHADER_PLATFORM = 14

# Mappings confirmed from real Android/iOS MLBB bundles during manual analysis.
DEFAULT_VERIFIED_MAPPINGS = {
    -6323308603036859518: 4275798749324902144,   # Card/UIClipCard_VX
    373315258768871810: -261248505249825764,    # ParticleFx_Pa_Blend_ND
}

# Android embedded shader names that need an external Metal shader on iOS.
DEFAULT_EMBEDDED_NAME_MAPPINGS = {
    "Custom/ToonShadowWithBlobOption_Fixed": 3758148330624623551,
}


class ConversionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShaderInfo:
    path_id: int
    name: str
    properties: frozenset[str]
    property_types: dict[str, int]
    platforms: tuple[int, ...]
    cab_uri: str
    source_bundle: str


@dataclass
class ShaderDecision:
    shader: ShaderInfo
    method: str
    score: float = 1.0


@dataclass
class ConversionStats:
    bundles: int = 0
    objects: int = 0
    materials: int = 0
    textures: int = 0
    exact_shaders: int = 0
    verified_remaps: int = 0
    property_matches: int = 0
    embedded_remaps: int = 0
    fallback_shaders: int = 0
    copied_files: int = 0
    warnings: list[str] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)


@dataclass
class ConvertOptions:
    create_zip: bool = True
    full_validation: bool = True
    strict_matching: bool = True
    preserve_shadows: bool = True


def app_directory() -> Path:
    # The toolkit bundles the mapping beside this module inside PyInstaller's
    # data directory, rather than beside the outer executable.
    return Path(__file__).resolve().parent


def path_with_ios_segments(path: Path) -> Path:
    return Path(*("ios" if part.lower() == "android" else part for part in path.parts))


def safe_name(value: str) -> str:
    value = re.sub(r"[<>:\"/\\|?*]+", "_", value).strip(" .")
    return value or "converted"


def unique_output_path(parent: Path, name: str, suffix: str = "") -> Path:
    candidate = parent / f"{name}{suffix}"
    number = 2
    while candidate.exists():
        candidate = parent / f"{name}_{number}{suffix}"
        number += 1
    return candidate


def cab_uri_for_asset_file(asset_file) -> str:
    name = str(asset_file.name).strip().lower()
    if not name.startswith("cab-"):
        raise ConversionError(
            f"Shader bundle internal file is not a CAB: {asset_file.name!r}"
        )
    return f"archive:/{name}/{name}"


def material_property_signature(material) -> tuple[set[str], dict[str, str]]:
    saved = material.m_SavedProperties
    names: set[str] = set()
    kinds: dict[str, str] = {}
    for name, _ in saved.m_TexEnvs:
        names.add(name)
        kinds[name] = "texture"
    for name, _ in saved.m_Floats:
        names.add(name)
        kinds[name] = "number"
    for name, _ in saved.m_Colors:
        names.add(name)
        kinds[name] = "vector"
    return names, kinds


def shader_kind(shader_type: int) -> str:
    if shader_type == 4:
        return "texture"
    if shader_type in (0, 1):
        return "vector"
    return "number"


def semantic_bonus(material, shader: ShaderInfo, used: set[str]) -> float:
    name = shader.name.lower()
    bonus = 0.0
    if {"_PanelClipInfo", "_PanelRect"} & used:
        bonus += 0.08 if ("ui" in name or "card" in name) else -0.05
    if "_Diffuse" in used:
        bonus += 0.05 if ("effect" in name or "particle" in name or "<effect>" in name) else 0
    if any("shadow" in prop.lower() for prop in used):
        bonus += 0.04 if "shadow" in name else 0
    keywords = str(getattr(material, "m_ShaderKeywords", "")).upper()
    if "ADDITIVE" in keywords or "_ADD" in keywords:
        bonus += 0.03 if "add" in name else -0.02
    return bonus


class ShaderCatalog:
    def __init__(self, shader_source: Path, logger: Callable[[str], None]):
        self.shader_source = shader_source
        self.log = logger
        self.by_path_id: dict[int, list[ShaderInfo]] = {}
        self.by_name: dict[str, list[ShaderInfo]] = {}
        self.verified_mappings = dict(DEFAULT_VERIFIED_MAPPINGS)
        self.embedded_name_mappings = dict(DEFAULT_EMBEDDED_NAME_MAPPINGS)
        self._load_optional_mappings()
        self._scan()

    def _load_optional_mappings(self):
        mapping_file = app_directory() / "verified_shader_mappings.json"
        if not mapping_file.is_file():
            return
        try:
            data = json.loads(mapping_file.read_text(encoding="utf-8"))
            for old, new in data.get("path_id_mappings", {}).items():
                self.verified_mappings[int(old)] = int(new)
            for name, new in data.get("embedded_shader_name_mappings", {}).items():
                self.embedded_name_mappings[str(name)] = int(new)
        except Exception as exc:
            raise ConversionError(f"Invalid verified_shader_mappings.json: {exc}") from exc

    def _shader_files(self) -> list[Path]:
        source = self.shader_source
        if source.is_file():
            return [source]
        if not source.is_dir():
            raise ConversionError(f"ShaderVariants path does not exist: {source}")
        preferred = [
            path for path in source.rglob("*.unity3d")
            if "shader" in path.name.lower()
        ]
        if preferred:
            return sorted(preferred)
        raise ConversionError(
            "No ShaderVariants .unity3d files were found in the selected folder."
        )

    def _scan(self):
        files = self._shader_files()
        self.log(f"Scanning {len(files)} iOS shader bundle(s)...")
        total = 0
        for path in files:
            try:
                env = UnityPy.load(str(path))
            except Exception as exc:
                raise ConversionError(f"Cannot open shader bundle {path.name}: {exc}") from exc
            found = 0
            for obj in env.objects:
                if obj.type.name != "Shader":
                    continue
                data = obj.read()
                platforms = tuple(int(value) for value in data.platforms)
                if METAL_SHADER_PLATFORM not in platforms:
                    continue
                parsed = data.m_ParsedForm
                prop_info = getattr(parsed, "m_PropInfo", None)
                props = tuple(prop_info.m_Props) if prop_info else ()
                info = ShaderInfo(
                    path_id=obj.path_id,
                    name=str(parsed.m_Name),
                    properties=frozenset(prop.m_Name for prop in props),
                    property_types={prop.m_Name: int(prop.m_Type) for prop in props},
                    platforms=platforms,
                    cab_uri=cab_uri_for_asset_file(obj.assets_file),
                    source_bundle=path.name,
                )
                self.by_path_id.setdefault(info.path_id, []).append(info)
                self.by_name.setdefault(info.name.lower(), []).append(info)
                found += 1
                total += 1
            self.log(f"  {path.name}: {found} Metal shaders")
        if not total:
            raise ConversionError("The selected ShaderVariants contains no Metal shaders.")
        self.log(f"Shader catalog ready: {total} Metal shaders")

    @staticmethod
    def _best_duplicate(items: list[ShaderInfo]) -> ShaderInfo:
        return sorted(items, key=lambda item: (len(item.properties), item.source_bundle), reverse=True)[0]

    def exact(self, path_id: int) -> ShaderInfo | None:
        items = self.by_path_id.get(path_id)
        return self._best_duplicate(items) if items else None

    def by_exact_name(self, name: str) -> ShaderInfo | None:
        items = self.by_name.get(name.lower())
        return self._best_duplicate(items) if items else None

    def decide(self, material, embedded_shader_name: str | None) -> ShaderDecision | None:
        old_path_id = int(material.m_Shader.path_id)
        exact = self.exact(old_path_id)
        if exact:
            return ShaderDecision(exact, "exact_path_id")

        verified_path_id = self.verified_mappings.get(old_path_id)
        if verified_path_id is not None:
            shader = self.exact(verified_path_id)
            if shader:
                return ShaderDecision(shader, "verified_mapping")

        if embedded_shader_name:
            exact_name = self.by_exact_name(embedded_shader_name)
            if exact_name:
                return ShaderDecision(exact_name, "embedded_exact_name")
            mapped = self.embedded_name_mappings.get(embedded_shader_name)
            if mapped is not None and self.exact(mapped):
                return ShaderDecision(self.exact(mapped), "embedded_verified_mapping")
            if embedded_shader_name == "Standard":
                keywords = str(getattr(material, "m_ShaderKeywords", "")).upper()
                tags = dict(getattr(material, "stringTagMap", []))
                transparent = (
                    "ALPHATEST" in keywords
                    or "TRANSPARENT" in str(tags.get("RenderType", "")).upper()
                )
                fallback_name = (
                    "Unlit/TextureColorAlpha"
                    if transparent else "Unlit/Unlit_Shadow"
                )
                shader = self.by_exact_name(fallback_name)
                if shader:
                    return ShaderDecision(shader, "embedded_standard_fallback", 0.90)

        # Some modified MLBB bundles contain Unity's generated Default-Material
        # with an intentionally empty shader PPtr (FileID=0, PathID=0). There is
        # no Android shader identifier to match in this case, so use a narrowly
        # scoped iOS fallback instead of treating it as an unknown real shader.
        if old_path_id == 0 and int(material.m_Shader.file_id) == 0:
            material_name = str(getattr(material, "m_Name", "")).strip().lower()
            if material_name in {"default-material", "default material"}:
                shader = self.by_exact_name("Unlit/TextureColorAlpha")
                if shader:
                    return ShaderDecision(shader, "null_default_invisible", 0.95)
            keywords = str(getattr(material, "m_ShaderKeywords", "")).upper()
            tags = dict(getattr(material, "stringTagMap", []))
            render_type = str(tags.get("RenderType", "")).upper()
            render_queue = int(getattr(material, "m_CustomRenderQueue", -1))
            transparent = (
                "ALPHATEST" in keywords
                or "TRANSPARENT" in keywords
                or "TRANSPARENT" in render_type
                or render_queue >= 2500
            )
            fallback_name = (
                "Unlit/TextureColorAlpha"
                if transparent else "Unlit/Unlit_Shadow"
            )
            shader = self.by_exact_name(fallback_name)
            if shader:
                return ShaderDecision(shader, "null_shader_fallback", 0.85)

        used, kinds = material_property_signature(material)
        if not used:
            return None
        ranked: list[tuple[float, int, float, ShaderInfo]] = []
        all_shaders = (
            self._best_duplicate(items) for items in self.by_path_id.values()
        )
        for shader in all_shaders:
            if not shader.properties:
                continue
            intersection = used & shader.properties
            if len(intersection) < 4:
                continue
            candidate_coverage = len(intersection) / len(shader.properties)
            material_coverage = len(intersection) / len(used)
            type_hits = sum(
                kinds[name] == shader_kind(shader.property_types[name])
                for name in intersection
            )
            type_coverage = type_hits / len(intersection)
            score = (
                0.62 * candidate_coverage
                + 0.28 * material_coverage
                + 0.10 * type_coverage
                + semantic_bonus(material, shader, used)
            )
            ranked.append((score, len(intersection), candidate_coverage, shader))
        if not ranked:
            return None
        ranked.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
        best = ranked[0]
        second_score = ranked[1][0] if len(ranked) > 1 else 0.0
        score, matched_count, candidate_coverage, shader = best
        confident = (
            candidate_coverage >= 0.80
            and matched_count >= 4
            and score >= 0.66
            and (score - second_score >= 0.015 or candidate_coverage >= 0.97)
        )
        if confident:
            return ShaderDecision(shader, "property_match", min(score, 1.0))
        return None


def rewrite_platform_strings(value):
    changed = False
    if isinstance(value, str):
        new = re.sub(r"(?i)([/\\])android([/\\])", r"\1ios\2", value)
        return new, new != value
    if isinstance(value, list):
        for index, item in enumerate(value):
            value[index], item_changed = rewrite_platform_strings(item)
            changed |= item_changed
        return value, changed
    if isinstance(value, dict):
        for key in list(value):
            value[key], item_changed = rewrite_platform_strings(value[key])
            changed |= item_changed
        return value, changed
    return value, False


def new_external_from_template(asset_file, cab_uri: str):
    if asset_file.externals:
        external = copy.copy(asset_file.externals[0])
    else:
        from UnityPy.files.SerializedFile import FileIdentifier
        external = FileIdentifier.__new__(FileIdentifier)
    external.temp_empty = ""
    external.guid = bytes(16)
    external.type = 0
    external.path = cab_uri
    return external


def ensure_external(asset_file, cab_uri: str) -> int:
    for index, external in enumerate(asset_file.externals, 1):
        if external.path.lower() == cab_uri.lower():
            return index
    asset_file.externals.append(new_external_from_template(asset_file, cab_uri))
    return len(asset_file.externals)


def add_shadow_defaults(material):
    saved = material.m_SavedProperties
    floats = dict(saved.m_Floats)
    colors = dict(saved.m_Colors)
    if "_Intensity" not in floats:
        saved.m_Floats.append(("_Intensity", 1.0))
    if "_Color" not in colors:
        saved.m_Colors.append(("_Color", ColorRGBA(1.0, 1.0, 1.0, 1.0)))
    if "_ShadowColor" not in colors:
        saved.m_Colors.append(("_ShadowColor", ColorRGBA(0.08, 0.08, 0.08, 0.72)))
    if "_ShadowPara" not in colors:
        saved.m_Colors.append(("_ShadowPara", ColorRGBA(1.0, 0.22, 0.0, 0.0)))
    if "_lightDir" not in colors:
        saved.m_Colors.append(("_lightDir", ColorRGBA(2.0, -2.7, -1.2, 0.0)))


def set_saved_color(material, name: str, color: ColorRGBA):
    colors = material.m_SavedProperties.m_Colors
    for index, (property_name, _) in enumerate(colors):
        if property_name == name:
            colors[index] = (name, color)
            return
    colors.append((name, color))


def prepare_material_for_shader(material, decision: ShaderDecision, preserve_shadows: bool):
    shader_name = decision.shader.name
    if shader_name == "Unlit/Unlit_Shadow" and preserve_shadows:
        add_shadow_defaults(material)
    if (
        decision.method in {
            "embedded_standard_fallback",
            "null_shader_fallback",
            "null_default_invisible",
        }
        and shader_name == "Unlit/TextureColorAlpha"
    ):
        material.m_ShaderKeywords = ""
        material.m_CustomRenderQueue = 3000
        if hasattr(material, "stringTagMap"):
            material.stringTagMap = [("RenderType", "Transparent")]
    if decision.method == "null_default_invisible":
        # A shaderless Default-Material is commonly attached to a helper or
        # shadow quad. An opaque fallback exposes it as a dark rectangle.
        transparent = ColorRGBA(1.0, 1.0, 1.0, 0.0)
        set_saved_color(material, "_Color", transparent)
        set_saved_color(material, "_TintColor", transparent)


def save_unity_environment(env, source: Path) -> tuple[bytes, bool]:
    """Return serialized bytes and whether the file was converted.

    Some MLBB packages use the .unity3d extension for a stream/resource companion
    rather than a Unity bundle. UnityPy exposes those as a binary reader, which has
    no save method. Preserve object-free companions byte-for-byte. For real assets,
    locate the serializable root through the edited objects instead of assuming
    env.file is always the bundle container.
    """
    objects = list(env.objects)
    roots: dict[int, object] = {}
    for obj in objects:
        node = obj.assets_file
        while True:
            parent = getattr(node, "parent", None)
            if parent is None or parent is env:
                break
            node = parent
        if callable(getattr(node, "save", None)):
            roots[id(node)] = node

    direct = getattr(env, "file", None)
    if callable(getattr(direct, "save", None)):
        roots[id(direct)] = direct

    if len(roots) == 1:
        root = next(iter(roots.values()))
        return root.save(packer="lz4"), True
    if not objects and not roots:
        return source.read_bytes(), False
    if not roots:
        raise ConversionError(
            f"Unity file contains editable objects but has no writable container: {source}"
        )
    raise ConversionError(
        f"Unity file contains multiple writable root containers and cannot be saved "
        f"as one file: {source}"
    )


def archive_stem(path: Path) -> str:
    name = path.name
    for suffix in (".tar.gz", ".tar.xz", ".zip", ".rar", ".7z"):
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def safe_extract_zip(source: Path, destination: Path):
    with zipfile.ZipFile(source) as archive:
        bad = archive.testzip()
        if bad:
            raise ConversionError(f"ZIP is corrupted at: {bad}")
        for info in archive.infolist():
            relative = PurePosixPath(info.filename)
            mode = (info.external_attr >> 16) & 0xFFFF
            if relative.is_absolute() or ".." in relative.parts or stat.S_ISLNK(mode):
                raise ConversionError(f"Unsafe ZIP entry refused: {info.filename}")
            target = destination.joinpath(*relative.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)


def safe_extract_rar(source: Path, destination: Path):
    try:
        import rarfile
    except ImportError as exc:
        raise ConversionError(
            "RAR support needs the 'rarfile' package. Run run_converter.bat first."
        ) from exc
    try:
        with rarfile.RarFile(source) as archive:
            for info in archive.infolist():
                relative = PurePosixPath(info.filename)
                if relative.is_absolute() or ".." in relative.parts or info.is_symlink():
                    raise ConversionError(f"Unsafe RAR entry refused: {info.filename}")
                target = destination.joinpath(*relative.parts)
                if info.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
    except rarfile.RarCannotExec as exc:
        raise ConversionError(
            "RAR extraction needs 7-Zip or UnRAR installed and available in PATH."
        ) from exc
    except rarfile.Error as exc:
        raise ConversionError(f"Cannot extract RAR: {exc}") from exc


def prepare_input(input_path: Path, work_root: Path, log: Callable[[str], None]) -> Path:
    if input_path.is_dir():
        return input_path
    extract_dir = work_root / "extracted"
    extract_dir.mkdir()
    suffix = input_path.suffix.lower()
    log(f"Extracting {input_path.name}...")
    if suffix == ".zip":
        safe_extract_zip(input_path, extract_dir)
    elif suffix == ".rar":
        safe_extract_rar(input_path, extract_dir)
    else:
        raise ConversionError("Input must be a folder, ZIP, or RAR archive.")
    return extract_dir


class Converter:
    def __init__(
        self,
        input_path: Path,
        shader_source: Path,
        output_parent: Path,
        options: ConvertOptions,
        logger: Callable[[str], None] = print,
        progress: Callable[[float, str], None] | None = None,
    ):
        self.input_path = input_path.resolve()
        self.shader_source = shader_source.resolve()
        self.output_parent = output_parent.resolve()
        self.options = options
        self.log = logger
        self.progress = progress or (lambda value, message: None)
        self.stats = ConversionStats()
        self.catalog: ShaderCatalog | None = None
        self.jobs: list[tuple[Path, Path]] = []

    def emit_progress(self, value: float, message: str):
        self.progress(max(0.0, min(100.0, value)), message)

    def convert(self) -> tuple[Path, Path | None, Path]:
        if not self.input_path.exists():
            raise ConversionError(f"Input does not exist: {self.input_path}")
        if not self.shader_source.exists():
            raise ConversionError(f"ShaderVariants does not exist: {self.shader_source}")
        self.output_parent.mkdir(parents=True, exist_ok=True)
        base = safe_name(archive_stem(self.input_path)) + "_iOS"
        number = 1
        while True:
            output_name = base if number == 1 else f"{base}_{number}"
            output_dir = self.output_parent / output_name
            candidate_zip = output_dir.with_name(output_dir.name + ".zip")
            if not output_dir.exists() and (
                not self.options.create_zip or not candidate_zip.exists()
            ):
                break
            number += 1
        output_zip = candidate_zip if self.options.create_zip else None
        report_path = output_dir / "conversion_report.json"

        self.emit_progress(1, "Preparing input")
        with tempfile.TemporaryDirectory(prefix="mlbb_ios_converter_") as temp_name:
            source_root = prepare_input(self.input_path, Path(temp_name), self.log)
            self.catalog = ShaderCatalog(self.shader_source, self.log)
            self.emit_progress(5, "Scanning Unity bundles")
            sources = sorted(source_root.rglob("*.unity3d"))
            if not sources:
                raise ConversionError("No .unity3d bundles were found in the selected input.")
            self.log(f"Found {len(sources)} Unity bundle(s)")
            output_dir.mkdir()
            try:
                self._copy_non_unity(source_root, output_dir)
                for number, source in enumerate(sources, 1):
                    relative = source.relative_to(source_root)
                    target = output_dir / path_with_ios_segments(relative)
                    self._convert_bundle(source, target)
                    self.jobs.append((source, target))
                    percent = 5 + (number / len(sources)) * 70
                    self.emit_progress(percent, f"Converting {number}/{len(sources)}")
                self._validate_all()
                self._write_report(report_path)
                if output_zip:
                    self.emit_progress(96, "Creating ZIP")
                    self._create_zip(output_dir, output_zip)
                self.emit_progress(100, "Complete")
            except Exception:
                shutil.rmtree(output_dir, ignore_errors=True)
                if output_zip and output_zip.exists():
                    output_zip.unlink()
                raise
        return output_dir, output_zip, report_path

    def _copy_non_unity(self, source_root: Path, output_dir: Path):
        for source in sorted(source_root.rglob("*")):
            relative = path_with_ios_segments(source.relative_to(source_root))
            target = output_dir / relative
            if source.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            elif source.suffix.lower() != ".unity3d":
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
                self.stats.copied_files += 1

    def _convert_bundle(self, source: Path, target: Path):
        assert self.catalog is not None
        try:
            env = UnityPy.load(str(source))
        except Exception as exc:
            raise ConversionError(f"Cannot open bundle {source}: {exc}") from exc
        self.stats.bundles += 1
        self.stats.objects += len(env.objects)
        embedded_names: dict[int, str] = {}
        for obj in env.objects:
            if obj.type.name == "Shader":
                data = obj.read()
                embedded_names[obj.path_id] = str(data.m_ParsedForm.m_Name)
        asset_files = {id(obj.assets_file): obj.assets_file for obj in env.objects}.values()
        for asset_file in asset_files:
            asset_file.target_platform = IOS_BUILD_TARGET
            asset_file._m_target_platform = IOS_BUILD_TARGET

        unresolved: list[str] = []
        for obj in env.objects:
            if obj.type.name == "Material":
                material = obj.read()
                old_file_id = int(material.m_Shader.file_id)
                old_path_id = int(material.m_Shader.path_id)
                embedded_name = embedded_names.get(old_path_id) if old_file_id == 0 else None
                decision = self.catalog.decide(material, embedded_name)
                if decision is None:
                    unresolved.append(
                        f"{material.m_Name} (FileID={old_file_id}, PathID={old_path_id})"
                    )
                    continue
                prepare_material_for_shader(
                    material, decision, self.options.preserve_shadows
                )
                if old_file_id > len(obj.assets_file.externals):
                    raise ConversionError(
                        f"Invalid shader FileID in {source}: "
                        f"{material.m_Name} ({old_file_id})"
                    )
                if old_file_id > 0:
                    file_id = old_file_id
                    obj.assets_file.externals[file_id - 1].path = decision.shader.cab_uri
                else:
                    file_id = ensure_external(obj.assets_file, decision.shader.cab_uri)
                material.m_Shader.m_FileID = file_id
                material.m_Shader.m_PathID = decision.shader.path_id
                obj.save_typetree(material)
                self.stats.materials += 1
                if decision.method == "exact_path_id":
                    self.stats.exact_shaders += 1
                elif decision.method == "property_match":
                    self.stats.property_matches += 1
                elif decision.method in {"null_shader_fallback", "null_default_invisible"}:
                    self.stats.fallback_shaders += 1
                elif decision.method.startswith("embedded"):
                    self.stats.embedded_remaps += 1
                else:
                    self.stats.verified_remaps += 1
                self.stats.decisions.append({
                    "bundle": str(path_with_ios_segments(source)),
                    "material": str(material.m_Name),
                    "old_file_id": old_file_id,
                    "old_path_id": old_path_id,
                    "new_file_id": file_id,
                    "new_path_id": decision.shader.path_id,
                    "shader_name": decision.shader.name,
                    "method": decision.method,
                    "confidence": round(decision.score, 4),
                    "shader_bundle": decision.shader.source_bundle,
                })
            elif obj.type.name == "Texture2D":
                try:
                    texture = obj.read()
                    image = texture.image.convert("RGBA")
                    texture.set_image(
                        image,
                        target_format=TextureFormat.RGBA32,
                        mipmap_count=1,
                    )
                    obj.save_typetree(texture)
                    self.stats.textures += 1
                except Exception as exc:
                    message = f"Texture conversion failed in {source.name}: {exc}"
                    if self.options.strict_matching:
                        raise ConversionError(message) from exc
                    self.stats.warnings.append(message)
                    self.log("WARNING: " + message)
            else:
                try:
                    tree = obj.read_typetree()
                    tree, changed = rewrite_platform_strings(tree)
                    if changed:
                        obj.save_typetree(tree)
                except (NotImplementedError, ValueError):
                    pass
        if unresolved:
            details = "\n  ".join(unresolved)
            raise ConversionError(
                f"Unresolved shader(s) in {source}:\n  {details}\n"
                "Use a matching iOS ShaderVariants bundle or add a verified mapping."
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        temp_target = target.with_name(target.name + ".tmp")
        try:
            output_bytes, converted = save_unity_environment(env, source)
            temp_target.write_bytes(output_bytes)
            os.replace(temp_target, target)
            if not converted:
                message = (
                    f"Preserved stream/resource companion unchanged: {source.name}"
                )
                self.log(message)
                self.stats.warnings.append(message)
        finally:
            if temp_target.exists():
                temp_target.unlink()

    def _validate_all(self):
        assert self.catalog is not None
        self.log("Validating converted bundles...")
        for number, (source, target) in enumerate(self.jobs, 1):
            source_env = UnityPy.load(str(source))
            output_env = UnityPy.load(str(target))
            source_identity = {(o.path_id, o.type.name) for o in source_env.objects}
            output_identity = {(o.path_id, o.type.name) for o in output_env.objects}
            if source_identity != output_identity:
                raise ConversionError(f"Object identity changed in {target.name}")
            for obj in output_env.objects:
                if int(obj.assets_file.target_platform) != IOS_BUILD_TARGET:
                    raise ConversionError(f"Wrong target platform in {target.name}")
                if obj.type.name == "Material":
                    material = obj.read()
                    path_id = int(material.m_Shader.path_id)
                    if path_id not in self.catalog.by_path_id:
                        raise ConversionError(
                            f"Missing iOS shader after conversion: {material.m_Name}"
                        )
                    file_id = int(material.m_Shader.file_id)
                    if file_id <= 0 or file_id > len(obj.assets_file.externals):
                        raise ConversionError(
                            f"Invalid shader dependency after conversion: {material.m_Name}"
                        )
                    actual_cab = obj.assets_file.externals[file_id - 1].path.lower()
                    valid_cabs = {
                        shader.cab_uri.lower()
                        for shader in self.catalog.by_path_id[path_id]
                    }
                    if actual_cab not in valid_cabs:
                        raise ConversionError(
                            f"Wrong ShaderVariants CAB after conversion: {material.m_Name}"
                        )
                elif obj.type.name == "Texture2D":
                    texture = obj.read()
                    if int(texture.m_TextureFormat) != int(TextureFormat.RGBA32):
                        raise ConversionError(f"Texture is not RGBA32: {texture.m_Name}")
                    stream = getattr(texture, "m_StreamData", None)
                    if stream and stream.path:
                        raise ConversionError(f"Texture stream remains: {texture.m_Name}")
                    if self.options.full_validation:
                        image = texture.image
                        if image.mode != "RGBA" or image.size != (
                            texture.m_Width,
                            texture.m_Height,
                        ):
                            raise ConversionError(f"Texture validation failed: {texture.m_Name}")
            percent = 76 + (number / len(self.jobs)) * 19
            self.emit_progress(percent, f"Validating {number}/{len(self.jobs)}")
            # UnityPy environments contain circular references. Explicitly
            # release them so Windows closes the files before cleanup.
            del source_env, output_env
            gc.collect()
        self.log("Validation passed")

    def _write_report(self, report_path: Path):
        report = {
            "application": APP_NAME,
            "version": APP_VERSION,
            "input": str(self.input_path),
            "shader_source": str(self.shader_source),
            "stats": {
                key: value
                for key, value in vars(self.stats).items()
                if key != "decisions"
            },
            "shader_decisions": self.stats.decisions,
        }
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def _create_zip(self, output_dir: Path, output_zip: Path):
        temp_zip = output_zip.with_name(output_zip.name + ".tmp")
        try:
            with zipfile.ZipFile(
                temp_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9
            ) as archive:
                for path in sorted(output_dir.rglob("*")):
                    name = path.relative_to(output_dir).as_posix()
                    if path.is_dir():
                        archive.writestr(name.rstrip("/") + "/", b"")
                    else:
                        archive.write(path, name)
            with zipfile.ZipFile(temp_zip) as archive:
                bad = archive.testzip()
                if bad:
                    raise ConversionError(f"Output ZIP failed CRC at {bad}")
            os.replace(temp_zip, output_zip)
        finally:
            if temp_zip.exists():
                temp_zip.unlink()


def parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--input", type=Path, help="Android folder, ZIP, or RAR")
    parser.add_argument("--shaders", type=Path, help="iOS ShaderVariants file/folder")
    parser.add_argument("--output", type=Path, help="Output parent folder")
    parser.add_argument("--no-zip", action="store_true", help="Do not create ZIP")
    parser.add_argument("--quick-validate", action="store_true")
    return parser.parse_args(argv)


def run_cli(args) -> int:
    if not args.input or not args.shaders or not args.output:
        print("--input, --shaders, and --output are required in CLI mode")
        return 2
    converter = Converter(
        args.input,
        args.shaders,
        args.output,
        ConvertOptions(
            create_zip=not args.no_zip,
            full_validation=not args.quick_validate,
        ),
        logger=print,
        progress=lambda value, message: print(f"[{value:5.1f}%] {message}"),
    )
    try:
        output_dir, output_zip, report = converter.convert()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"Output folder: {output_dir}")
    if output_zip:
        print(f"Output ZIP: {output_zip}")
    print(f"Report: {report}")
    return 0


def launch_gui():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    class ConverterGUI:
        BG = "#171a21"
        PANEL = "#222631"
        ENTRY = "#11141a"
        TEXT = "#eef1f7"
        MUTED = "#aab2c0"
        BLUE = "#4b8cff"
        GREEN = "#3ddc97"
        RED = "#ff6b6b"

        def __init__(self, root: tk.Tk):
            self.root = root
            self.root.title(f"{APP_NAME} v{APP_VERSION}")
            self.root.geometry("900x690")
            self.root.minsize(760, 600)
            self.root.configure(bg=self.BG)
            self.events: queue.Queue = queue.Queue()
            self.last_output: Path | None = None
            self._style()
            self._build()
            self.root.after(80, self._poll_events)

        def _style(self):
            style = ttk.Style()
            style.theme_use("clam")
            style.configure("TFrame", background=self.BG)
            style.configure("Panel.TFrame", background=self.PANEL)
            style.configure("TLabel", background=self.BG, foreground=self.TEXT)
            style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
            style.configure("Muted.TLabel", foreground=self.MUTED)
            style.configure("TCheckbutton", background=self.PANEL, foreground=self.TEXT)
            style.map("TCheckbutton", background=[("active", self.PANEL)])
            style.configure(
                "Accent.TButton",
                background=self.BLUE,
                foreground="white",
                font=("Segoe UI", 10, "bold"),
                padding=(14, 9),
            )
            style.map("Accent.TButton", background=[("active", "#67a0ff")])
            style.configure("TButton", padding=(10, 7))
            style.configure(
                "Horizontal.TProgressbar",
                troughcolor=self.ENTRY,
                background=self.GREEN,
                bordercolor=self.ENTRY,
                lightcolor=self.GREEN,
                darkcolor=self.GREEN,
            )

        def _build(self):
            outer = ttk.Frame(self.root, padding=20)
            outer.pack(fill="both", expand=True)
            ttk.Label(outer, text=APP_NAME, style="Title.TLabel").pack(anchor="w")
            ttk.Label(
                outer,
                text="Android → iOS Unity bundle converter with exact ShaderVariants matching",
                style="Muted.TLabel",
            ).pack(anchor="w", pady=(2, 14))

            panel = ttk.Frame(outer, style="Panel.TFrame", padding=16)
            panel.pack(fill="x")
            panel.columnconfigure(1, weight=1)
            self.input_var = tk.StringVar()
            self.shader_var = tk.StringVar()
            self.output_var = tk.StringVar(value=str(Path.home() / "Desktop"))
            self._path_row(panel, 0, "Android input", self.input_var, self._input_buttons)
            self._path_row(panel, 1, "iOS shaders", self.shader_var, self._shader_buttons)
            self._path_row(panel, 2, "Output folder", self.output_var, self._output_button)

            options = ttk.Frame(panel, style="Panel.TFrame")
            options.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(12, 0))
            self.zip_var = tk.BooleanVar(value=True)
            self.validate_var = tk.BooleanVar(value=True)
            self.shadow_var = tk.BooleanVar(value=True)
            ttk.Checkbutton(options, text="Create ZIP", variable=self.zip_var).pack(side="left", padx=(0, 18))
            ttk.Checkbutton(options, text="Full texture validation", variable=self.validate_var).pack(side="left", padx=(0, 18))
            ttk.Checkbutton(options, text="Preserve shadows", variable=self.shadow_var).pack(side="left")

            controls = ttk.Frame(outer, padding=(0, 14, 0, 8))
            controls.pack(fill="x")
            self.convert_button = ttk.Button(
                controls, text="CONVERT TO iOS", style="Accent.TButton", command=self._start
            )
            self.convert_button.pack(side="left")
            self.open_button = ttk.Button(
                controls, text="Open Output", command=self._open_output, state="disabled"
            )
            self.open_button.pack(side="left", padx=8)
            self.status_var = tk.StringVar(value="Ready")
            ttk.Label(controls, textvariable=self.status_var, style="Muted.TLabel").pack(side="right")

            self.progress_var = tk.DoubleVar()
            ttk.Progressbar(outer, variable=self.progress_var, maximum=100).pack(fill="x", pady=(0, 10))
            self.log_box = tk.Text(
                outer,
                bg=self.ENTRY,
                fg=self.TEXT,
                insertbackground=self.TEXT,
                relief="flat",
                font=("Consolas", 9),
                wrap="word",
                padx=10,
                pady=10,
            )
            self.log_box.pack(fill="both", expand=True)
            self.log_box.configure(state="disabled")

        def _path_row(self, parent, row, label, variable, button_builder):
            ttk.Label(parent, text=label, style="Muted.TLabel").grid(
                row=row, column=0, sticky="w", padx=(0, 12), pady=6
            )
            entry = tk.Entry(
                parent,
                textvariable=variable,
                bg=self.ENTRY,
                fg=self.TEXT,
                insertbackground=self.TEXT,
                relief="flat",
                font=("Segoe UI", 10),
            )
            entry.grid(row=row, column=1, sticky="ew", ipady=7, pady=6)
            holder = ttk.Frame(parent, style="Panel.TFrame")
            holder.grid(row=row, column=2, sticky="e", padx=(8, 0))
            button_builder(holder)

        def _input_buttons(self, parent):
            ttk.Button(parent, text="Folder", command=self._browse_input_folder).pack(side="left")
            ttk.Button(parent, text="ZIP/RAR", command=self._browse_input_file).pack(side="left", padx=(5, 0))

        def _shader_buttons(self, parent):
            ttk.Button(parent, text="File", command=self._browse_shader_file).pack(side="left")
            ttk.Button(parent, text="Folder", command=self._browse_shader_folder).pack(side="left", padx=(5, 0))

        def _output_button(self, parent):
            ttk.Button(parent, text="Browse", command=self._browse_output).pack()

        def _browse_input_folder(self):
            value = filedialog.askdirectory(title="Select Android bundle folder")
            if value: self.input_var.set(value)

        def _browse_input_file(self):
            value = filedialog.askopenfilename(
                title="Select Android ZIP or RAR",
                filetypes=[("Archives", "*.zip *.rar"), ("All files", "*.*")],
            )
            if value: self.input_var.set(value)

        def _browse_shader_file(self):
            value = filedialog.askopenfilename(
                title="Select iOS ShaderVariants",
                filetypes=[("Unity bundles", "*.unity3d"), ("All files", "*.*")],
            )
            if value: self.shader_var.set(value)

        def _browse_shader_folder(self):
            value = filedialog.askdirectory(title="Select iOS ShaderVariants folder")
            if value: self.shader_var.set(value)

        def _browse_output(self):
            value = filedialog.askdirectory(title="Select output folder")
            if value: self.output_var.set(value)

        def _append_log(self, message: str):
            self.log_box.configure(state="normal")
            self.log_box.insert("end", message.rstrip() + "\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")

        def _start(self):
            input_path = Path(self.input_var.get().strip())
            shader_path = Path(self.shader_var.get().strip())
            output_path = Path(self.output_var.get().strip())
            if not input_path.exists():
                messagebox.showerror(APP_NAME, "Select a valid Android folder, ZIP, or RAR.")
                return
            if not shader_path.exists():
                messagebox.showerror(APP_NAME, "Select a valid iOS ShaderVariants file or folder.")
                return
            self.convert_button.configure(state="disabled")
            self.open_button.configure(state="disabled")
            self.progress_var.set(0)
            self.status_var.set("Starting...")
            self._append_log("=" * 64)
            self._append_log(f"{APP_NAME} v{APP_VERSION}")
            option_values = ConvertOptions(
                create_zip=self.zip_var.get(),
                full_validation=self.validate_var.get(),
                preserve_shadows=self.shadow_var.get(),
            )
            worker = threading.Thread(
                target=self._worker,
                args=(input_path, shader_path, output_path, option_values),
                daemon=True,
            )
            worker.start()

        def _worker(self, input_path, shader_path, output_path, option_values):
            converter = Converter(
                input_path,
                shader_path,
                output_path,
                option_values,
                logger=lambda message: self.events.put(("log", message)),
                progress=lambda value, message: self.events.put(("progress", value, message)),
            )
            try:
                output_dir, output_zip, report = converter.convert()
                self.events.put(("done", output_dir, output_zip, report, converter.stats))
            except Exception as exc:
                self.events.put(("error", str(exc), traceback.format_exc()))

        def _poll_events(self):
            try:
                while True:
                    event = self.events.get_nowait()
                    kind = event[0]
                    if kind == "log":
                        self._append_log(event[1])
                    elif kind == "progress":
                        self.progress_var.set(event[1])
                        self.status_var.set(event[2])
                    elif kind == "done":
                        _, output_dir, output_zip, report, stats = event
                        self.last_output = output_zip or output_dir
                        self.convert_button.configure(state="normal")
                        self.open_button.configure(state="normal")
                        self.status_var.set("Complete")
                        self._append_log(
                            f"COMPLETE: {stats.bundles} bundles, {stats.materials} materials, "
                            f"{stats.textures} textures"
                        )
                        self._append_log(f"Output: {self.last_output}")
                        self._append_log(f"Report: {report}")
                        messagebox.showinfo(
                            APP_NAME,
                            f"Conversion complete.\n\nBundles: {stats.bundles}\n"
                            f"Materials: {stats.materials}\nTextures: {stats.textures}\n\n"
                            f"Saved to:\n{self.last_output}",
                        )
                    elif kind == "error":
                        self.convert_button.configure(state="normal")
                        self.status_var.set("Failed")
                        self._append_log("ERROR: " + event[1])
                        self._append_log(event[2])
                        messagebox.showerror(APP_NAME, event[1])
            except queue.Empty:
                pass
            self.root.after(80, self._poll_events)

        def _open_output(self):
            if not self.last_output:
                return
            target = self.last_output.parent if self.last_output.is_file() else self.last_output
            if os.name == "nt":
                os.startfile(target)
            elif sys.platform == "darwin":
                os.system(f'open "{target}"')
            else:
                os.system(f'xdg-open "{target}"')

    root = tk.Tk()
    ConverterGUI(root)
    root.mainloop()


def main() -> int:
    args = parse_args()
    if args.input or args.shaders or args.output:
        return run_cli(args)
    launch_gui()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
