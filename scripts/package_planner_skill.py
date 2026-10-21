"""Export the planner skill and portable instructions without running a task."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills/generalrouter-task-profile"
MATERIALS = ROOT / "项目资料"
EXPORT_VERSION = "0.7"


def main() -> None:
    MATERIALS.mkdir(parents=True, exist_ok=True)
    formats = MATERIALS / f"provider-formats-v{EXPORT_VERSION}"
    formats.mkdir(parents=True, exist_ok=True)
    builder = SKILL / "scripts/build_prompt.py"
    prompt = MATERIALS / f"planner-prompt-v{EXPORT_VERSION}.md"
    subprocess.run(
        [sys.executable, str(builder), "--output", str(prompt)], check=True
    )
    for provider in (
        "schema",
        "openai-responses",
        "openai-chat",
        "claude-messages",
        "gemini-interactions",
    ):
        subprocess.run(
            [
                sys.executable,
                str(builder),
                "--provider-format",
                provider,
                "--output",
                str(formats / f"{provider}.json"),
            ],
            check=True,
        )
    archive_path = MATERIALS / f"generalrouter-task-profile-v{EXPORT_VERSION}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(SKILL.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".pyo"}:
                archive.write(path, path.relative_to(SKILL.parent))
    shutil.copy2(
        SKILL / "references/calibration.json",
        MATERIALS / "GeneralRouter-v0.1-四维任务参考拟合.json",
    )
    print(json.dumps({
        "skill_archive": str(archive_path),
        "portable_prompt": str(prompt),
        "provider_formats": str(formats),
        "tasks_executed": False,
        "models_called": False,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
