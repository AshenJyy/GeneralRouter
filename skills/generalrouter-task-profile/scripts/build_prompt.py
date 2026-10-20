"""Assemble portable planner instructions or a provider output-format fragment.

Only writes local text/JSON. Makes no requests and does not execute task examples.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]


def skill_body(text: str) -> str:
    lines = text.lstrip("\ufeff").splitlines()
    if lines and lines[0].strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return "\n".join(lines[index + 1:]).strip()
    return text.strip()


def build_prompt(include_examples: bool = False, include_references: bool = False) -> str:
    sections = [
        "# GeneralRouter Planner Contract\n\n"
        "Use the compact contract below to prepare executable subtasks. Its definitions "
        "and semantic-anchor table are sufficient for ordinary planning; filesystem "
        "access and skill discovery are not required. Supporting references are embedded "
        "only when explicitly included below. Do not claim to have read an omitted "
        "historical reference; obtain it before using an AA-anchor source. Treat example "
        "requests as examples, not tasks to execute. As a standalone planner, return "
        "only a task-profile.v3 JSON object. Native-host dispatch instructions apply "
        "only when that host actually provides the corresponding tools.",
        skill_body((SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8-sig")),
    ]
    references = []
    if include_references:
        references.extend(["capability-rubric.md", "semantic-anchors.json", "task-profile.schema.json"])
    if include_examples:
        references.extend(["planner-anchors.json", "examples.json"])
    for name in references:
        content = (SKILL_ROOT / "references" / name).read_text(encoding="utf-8-sig")
        if name.endswith(".json"):
            content = "```json\n" + content.rstrip() + "\n```"
        sections.append(f"# Loaded reference: {name}\n\n{content}")
    return "\n\n---\n\n".join(sections) + "\n"


def provider_fragment(provider: str) -> dict:
    schema = json.loads((SKILL_ROOT / "references" / "task-profile.schema.json").read_text(encoding="utf-8-sig"))
    if provider == "openai-responses":
        return {"text": {"format": {"type": "json_schema", "name": "generalrouter_task_profile", "strict": True, "schema": schema}}}
    if provider == "openai-chat":
        return {"response_format": {"type": "json_schema", "json_schema": {"name": "generalrouter_task_profile", "strict": True, "schema": schema}}}
    if provider == "claude-messages":
        return {"output_config": {"format": {"type": "json_schema", "schema": schema}}}
    if provider == "gemini-interactions":
        return {"response_format": {"type": "text", "mime_type": "application/json", "schema": schema}}
    if provider == "schema":
        return schema
    raise ValueError(f"Unknown provider format: {provider}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", "-o", default="-")
    parser.add_argument("--provider-format", choices=("schema", "openai-responses", "openai-chat", "claude-messages", "gemini-interactions"))
    parser.add_argument("--include-references", action="store_true", help="Embed the expanded rubric, semantic-anchor records, and JSON Schema; the default prompt uses only the compact contract")
    parser.add_argument("--include-examples", action="store_true", help="Embed the optional historical AA fitted anchors and their task examples")
    args = parser.parse_args()
    content = (json.dumps(provider_fragment(args.provider_format), ensure_ascii=False, indent=2) + "\n") if args.provider_format else build_prompt(include_examples=args.include_examples, include_references=args.include_references)
    if args.output == "-":
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(content)
    else:
        Path(args.output).write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
