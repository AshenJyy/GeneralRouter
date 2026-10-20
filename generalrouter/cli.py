"""Return model choices to the main agent through a JSON tool result."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .config import load_config
from .runner import GeneralRouter


def main() -> int:
    parser = argparse.ArgumentParser(
        description="GeneralRouter：读取 Planner 向量，选择模型，将执行任务交回主 agent。",
    )
    parser.add_argument("--profile", required=True, help="四维 task-profile.v3 JSON 文件；- 表示从 stdin 读取")
    parser.add_argument("--request", default="", help="需要随执行任务保留的原始用户请求")
    parser.add_argument("--request-file", type=Path, help="从 UTF-8 文件读取原始用户请求")
    parser.add_argument("--output", "-o", default="-", help="交接 JSON 输出位置，默认 stdout，供主 agent 读取")
    parser.add_argument("--config", type=Path, help="可选路由配置文件")
    parser.add_argument("--intelligence-prior", type=float, help="AA 四类综合先验的混合占比，范围 0–1，默认 0.4")
    parser.add_argument("--missing-penalty", type=float, help="缺失数据的最高折扣比例，范围 0–1，默认 0.25")
    parser.add_argument("--preferred-configuration", help="同分时优先选择的模型配置 ID")
    args = parser.parse_args()
    if args.request and args.request_file is not None:
        parser.error("--request 与 --request-file 只能使用一个")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        config = load_config(args.config)
        for key in ("intelligence_prior", "missing_penalty"):
            value = getattr(args, key)
            if value is not None:
                if not 0 <= value <= 1:
                    raise ValueError(f"{key} must be between 0 and 1")
                config[key] = value
        if args.preferred_configuration is not None:
            config["preferred_configuration"] = args.preferred_configuration
        router = GeneralRouter(config)
        raw = sys.stdin.read() if args.profile == "-" else Path(args.profile).read_text(encoding="utf-8-sig")
        profile = router.skill.parse(raw)
        request = args.request_file.read_text(encoding="utf-8-sig") if args.request_file else args.request
        handoff = router.route(profile, original_request=request)
        serialized = json.dumps(handoff, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        if args.output == "-":
            sys.stdout.write(serialized)
        else:
            Path(args.output).write_text(serialized, encoding="utf-8")
        return 0
    except (OSError, ValueError, TypeError, KeyError, ImportError) as exc:
        print(f"GeneralRouter：{exc}", file=sys.stderr)
        return 2
