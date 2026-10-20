"""Write the self-contained, prebuilt DSH bundle tarball and companion files.

This is artifact generation only. It never imports plugin/runtime modules, runs
package lifecycle scripts, installs anything, or invokes models. The tarball
uses npm's conventional package/ prefix and the public package manifest.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
import tarfile


PROJECT = Path(__file__).resolve().parents[1]
PLUGIN = PROJECT / "dsh-generalrouter"


def public_files() -> list[tuple[Path, str]]:
    files = [
        (PLUGIN / name, name)
        for name in (
            "package.json",
            "index.mjs",
            "recording-diagnostics.mjs",
            "cordis.patch.yml",
            "Install-Desktop.ps1",
            "README.zh-CN.md",
        )
    ]
    files += [
        (PROJECT / "host.example.json", "examples/host.example.json"),
        (PROJECT / "data/quota-policy.example.json", "examples/quota-policy.example.json"),
        (PROJECT / "config.example.json", "runtime/config.example.json"),
    ]
    files += [
        (PROJECT / "data" / name, f"runtime/data/{name}")
        for name in (
            "model-vectors.v0.1.json",
            "model-call-mappings.v0.1.json",
            "api-equivalent-rates.v0.1.json",
            "quota-policy.example.json",
        )
    ]
    for path in sorted((PROJECT / "generalrouter").glob("*.py")):
        files.append((path, f"runtime/generalrouter/{path.name}"))
    skill = PROJECT / "skills/generalrouter-task-profile"
    for path in sorted(skill.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        files.append((path, f"runtime/skills/generalrouter-task-profile/{path.relative_to(skill).as_posix()}"))
    return files


def bytes_for(path: Path) -> bytes:
    if path.suffix == ".ps1":
        # Windows PowerShell 5.1 needs the BOM for non-ASCII source text.
        return path.read_text(encoding="utf-8-sig").encode("utf-8-sig")
    return path.read_bytes()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=PROJECT)
    args = parser.parse_args()
    destination = args.output.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    metadata = json.loads((PLUGIN / "package.json").read_text(encoding="utf-8-sig"))
    package_name = metadata["name"].removeprefix("@").replace("/", "-")
    tarball = destination / f"{package_name}-{metadata['version']}.tgz"
    entries = public_files()
    with tarfile.open(tarball, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for path, relative in entries:
            content = bytes_for(path)
            entry = tarfile.TarInfo(f"package/{relative}")
            entry.size = len(content)
            entry.mode = 0o644
            entry.mtime = 0
            entry.uid = entry.gid = 0
            entry.uname = entry.gname = ""
            archive.addfile(entry, io.BytesIO(content))
    materials = destination / "项目资料"
    materials.mkdir(parents=True, exist_ok=True)
    (materials / "Install-Desktop.ps1").write_bytes(bytes_for(PLUGIN / "Install-Desktop.ps1"))
    (destination / "GeneralRouter-v0.1-DSH安装说明.txt").write_text(
        (PLUGIN / "README.zh-CN.md").read_text(encoding="utf-8-sig"), encoding="utf-8-sig"
    )
    templates = materials / "GeneralRouter-DSH-配置模板"
    templates.mkdir(parents=True, exist_ok=True)
    for source, name in entries:
        if name.startswith("examples/"):
            (templates / Path(name).name).write_bytes(bytes_for(source))
    print(json.dumps({"tarball": str(tarball), "files": len(entries), "installed": False, "runtime_executed": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
