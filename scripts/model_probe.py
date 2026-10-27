#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from lib.data_root import local_config_path
from lib.triage_agent.model_client import (
    OpenAICompatibleClient,
    load_model_config,
    probe_native_tools,
)


def run(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Probe model tool-call compatibility")
    parser.add_argument("--config", default=str(local_config_path(REPO_ROOT, "model.local.yaml")))
    args = parser.parse_args(argv)
    try:
        client = OpenAICompatibleClient(load_model_config(Path(args.config)))
        result = probe_native_tools(client)
        ok = all(result.values())
        print(json.dumps({"ok": ok, **result}, ensure_ascii=False))
        return 0 if ok else 1
    except Exception as exc:
        print(json.dumps({
            "ok": False,
            "error": type(exc).__name__,
            "message": str(exc),
        }, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
