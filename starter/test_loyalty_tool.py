#!/usr/bin/env python3
"""
Verifies calculate_loyalty_discount for BOTH paths, without AWS:
  1. success path (fake sandbox that really executes the generated code)
  2. forced Code Interpreter exception (fallback path)
Run from the project folder:  python test_loyalty_tool.py
Log:  test_logs/loyalty_tool_verification.txt
"""
import contextlib, importlib, io, json, os, sys
from pathlib import Path
from unittest.mock import MagicMock

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, ".")

# Stub heavy/optional packages that are not needed to test the pure tool logic.
for name in ("bedrock_agentcore", "bedrock_agentcore.runtime", "bedrock_agentcore.memory",
             "bedrock_agentcore.tools", "bedrock_agentcore.tools.code_interpreter_client",
             "strands_tools", "strands_tools.browser", "mcp", "mcp.client",
             "mcp.client.streamable_http"):
    try:
        importlib.import_module(name)
    except Exception:
        sys.modules[name] = MagicMock()

import main  # noqa: E402

REQUIRED = ("points_redeemed", "tier_discount_pct", "final_total", "remaining_points")
lines = []


def log(s=""):
    lines.append(s)
    print(s)


class FakeSession:
    """Runs the generated program locally and mimics the Code Interpreter envelope."""
    def __init__(self, mode="structured"):
        self.mode = mode

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def invoke(self, name, params):
        assert name == "executeCode" and params["language"] == "python" and params["clearContext"] is True
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(params["code"], {})
        out = buf.getvalue()
        result = {"content": [{"type": "text", "text": out}], "isError": False}
        if self.mode == "structured":
            result["structuredContent"] = {"stdout": out, "stderr": "", "exitCode": 0}
        return {"stream": [{"result": result}]}


def run(case, **kw):
    data = json.loads(main.calculate_loyalty_discount(**kw))
    missing = [k for k in REQUIRED if k not in data]
    assert not missing, f"{case}: missing keys {missing}"
    log(f"[{case}] {json.dumps(data)}")
    return data


# 1. success path (both envelope shapes)
for mode in ("structured", "text-only"):
    main.code_session = lambda region, m=mode: FakeSession(m)
    d = run(f"sandbox success ({mode})", loyalty_points=4250, tier="Gold", order_total=200.0, product_category="device")
    assert d["points_redeemed"] == 4000 and d["tier_discount_pct"] == 10.0
    assert d["final_total"] == 144.0 and d["remaining_points"] == 538 and d["fallback"] is False

d = run("sandbox Silver standard", loyalty_points=890, tier="Silver", order_total=100.0, product_category="standard")
assert d["tier_discount_pct"] == 0.0 and d["points_redeemed"] == 500 and d["final_total"] == 95.0
d = run("sandbox cap boundary", loyalty_points=100000, tier="Platinum", order_total=100.0, product_category="fresh")
assert d["points_redeemed"] == 5000 and d["tier_discount_pct"] == 15.0

# 2. forced interpreter exception -> fallback
def boom(region):
    raise RuntimeError("simulated Code Interpreter outage")

main.code_session = boom
d = run("fallback (forced exception)", loyalty_points=4250, tier="Gold", order_total=200.0, product_category="device")
assert d["fallback"] is True and d["points_redeemed"] == 0 and d["tier_discount_pct"] == 10.0
assert d["final_total"] == 180.0 and d["remaining_points"] == 4250 + 360

log("\nALL ASSERTIONS PASSED")
Path("test_logs").mkdir(exist_ok=True)
Path("test_logs/loyalty_tool_verification.txt").write_text("\n".join(lines), encoding="utf-8")
