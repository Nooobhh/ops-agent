"""真实环境 SSH 冒烟(手动跑,不进 CI):50 连发、超长单行、超时不毒化。

用法:.venv/bin/python tests/smoke_ssh.py --node node12
需要:config/local-dev.yaml(跳板+credentials_file)、config/platform-alpha.yaml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.cmd_filter import CmdFilter, default_rules_path       # noqa: E402
from lib.config import load_local_dev, load_platform           # noqa: E402
from lib.credentials import FileCredentials                    # noqa: E402
from lib.runner import run_playbook                            # noqa: E402
from lib.ssh_pool import SSHPool                                # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", required=True)
    ap.add_argument("--platform", default=str(REPO / "config" / "platform-alpha.yaml"))
    ap.add_argument("--local-dev", default=str(REPO / "config" / "local-dev.yaml"))
    a = ap.parse_args()

    dev = load_local_dev(Path(a.local_dev))
    pool = SSHPool(
        load_platform(Path(a.platform)),
        FileCredentials(dev.credentials_file.expanduser()) if dev else FileCredentials(Path("config/credentials.json")),
        CmdFilter.from_yaml(default_rules_path()),
        jump=dev.jump if dev else None,
    )
    failures = []

    # 1) 50 连发:输出完整性
    for i in range(50):
        r = pool.run(a.node, "seq 1 500 | tail -1")
        if r.stdout.strip() != "500" or r.exit_code != 0:
            failures.append(f"连发第{i}次异常: exit={r.exit_code} out={r.stdout[-80:]!r}")
    print(f"[1/3] 50 连发: {'FAIL' if failures else 'PASS'}")

    # 2) 超长单行(30KB,复刻 v3 被吞场景)
    r = pool.run(a.node, "awk 'BEGIN{s=\"\";for(i=0;i<30000;i++)s=s\"x\";print s}'")
    ok = len(r.stdout.strip()) == 30000
    print(f"[2/3] 30KB 单行: {'PASS' if ok else 'FAIL'} (len={len(r.stdout.strip())})")
    if not ok:
        failures.append("超长单行被截断")

    # 3) 超时不毒化:sleep 5 超时后,同 Transport 下一条命令正常
    r1 = pool.run(a.node, "sleep 5", timeout=2)
    r2 = pool.run(a.node, "echo alive")
    ok = r1.timed_out and r2.stdout.strip() == "alive"
    print(f"[3/3] 超时不毒化: {'PASS' if ok else 'FAIL'}")
    if not ok:
        failures.append(f"毒化检测: timed_out={r1.timed_out} next={r2.stdout!r}")

    # 4) 附加:toy playbook 远端真跑
    res = run_playbook(pool, a.node, REPO / "tests" / "data" / "toy_playbook.sh", run_id="smoke")
    print(f"[附加] 远端 toy playbook: verdict={res.verdict} steps={[s.pass_ for s in res.steps]}")

    pool.close()
    for f_ in failures:
        print("FAIL:", f_)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
