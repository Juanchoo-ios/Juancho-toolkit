from __future__ import annotations

import hashlib
from pathlib import Path


EXPECTED_SIZE = 28_539_020
EXPECTED_SHA256 = "49d9bdc5a85160c4d7638497d9135c647fb36813e425c4b7d40f7e33b672befa"


def digest(path: Path) -> str:
    checksum = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(block)
    return checksum.hexdigest()


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    folder = root / "src" / "bisaya_toolkit_ios" / "converter_engine" / "shaders"
    target = folder / "ShaderVariants_add.unity3d"
    parts = sorted(folder.glob("ShaderVariants_add.unity3d.part*"))
    if target.is_file() and target.stat().st_size == EXPECTED_SIZE and digest(target) == EXPECTED_SHA256:
        print(f"Shader already assembled and verified: {target}")
        return
    if not parts:
        raise SystemExit("Shader parts are missing.")
    temporary = target.with_suffix(".unity3d.tmp")
    with temporary.open("wb") as output:
        for part in parts:
            print(f"Adding {part.name}")
            with part.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(block)
    if temporary.stat().st_size != EXPECTED_SIZE or digest(temporary) != EXPECTED_SHA256:
        temporary.unlink(missing_ok=True)
        raise SystemExit("Reassembled ShaderVariants_add failed integrity verification.")
    temporary.replace(target)
    print(f"Shader assembled and verified: {target}")


if __name__ == "__main__":
    main()
