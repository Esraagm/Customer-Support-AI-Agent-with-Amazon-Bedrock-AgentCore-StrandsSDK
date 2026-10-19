#!/usr/bin/env python3
"""
Runs the 6 required tests against the deployed agent and saves logs.

  python run_tests.py            # all tests
  python run_tests.py 1 2        # only tests 1 and 2
Logs: test_logs/testN_*.txt  (includes exit code of each agentcore invoke)
"""
import json, os, subprocess, sys, time
from pathlib import Path

CUST = "CUST-123"
MEMORY_WAIT = 90  # seconds for async memory extraction

TESTS = {
    1: ("order_tracking", [("t1", "Where is my order ORD-001? Give me the tracking number, carrier and estimated delivery date.")]),
    2: ("refund_processing", [("t2", "Order ORD-002 (Kindle Paperwhite) arrived damaged. Please process a full refund and send me a return label. In your reply include the refund ID, refund amount, status, return label URL and carrier.")]),
    3: ("knowledge_base_rag", [("t3", "What is the return policy for electronics, and how long does a refund to a credit card take?")]),
    4: ("long_term_memory", [
        ("mem-session-a", "Hi, my name is Jane and I prefer to be contacted by email. I mostly shop for e-readers."),
        ("WAIT", MEMORY_WAIT),
        ("mem-session-b", "What is my name and how do I prefer to be contacted?"),
    ]),
    5: ("loyalty_discount", [("t5", "I'm a Gold member with 4250 points. Calculate my loyalty discount for a $200 order of Amazon devices.")]),
    6: ("browser_tool", [("t6", "Use the browser to open https://example.com and tell me the page's main heading.")]),
}

# Force UTF-8 inside the child agentcore process (Windows cp1252 cannot print emoji).
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "AGENTCORE_SUPPRESS_RECOMMENDATION": "1"}


def invoke(session, prompt):
    payload = {"prompt": prompt, "customer_id": CUST, "session_id": session}
    r = subprocess.run(
        ["agentcore", "invoke", json.dumps(payload)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", env=ENV,
    )
    return payload, r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    only = {int(a) for a in sys.argv[1:]} or set(TESTS)
    Path("test_logs").mkdir(exist_ok=True)
    for n in sorted(only):
        if n == 4:  # memory test has its own script (unique preference + extraction polling)
            subprocess.run([sys.executable, "run_memory_test.py"], env=ENV)
            continue
        name, steps = TESTS[n]
        lines = [f"=== Test {n}: {name} ===\n"]
        for session, prompt in steps:
            if session == "WAIT":
                print(f"   waiting {prompt}s for memory extraction...")
                time.sleep(prompt)
                continue
            payload, code, out = invoke(session, prompt)
            flag = "  <-- ERROR, rerun" if code != 0 or "UnicodeEncodeError" in out else ""
            lines.append(
                f"--- session: {session}\nREQUEST: {json.dumps(payload)}\n"
                f"EXIT CODE: {code}{flag}\nRESPONSE:\n{out}\n"
            )
        text = "\n".join(lines)
        Path(f"test_logs/test{n}_{name}.txt").write_text(text, encoding="utf-8")
        print(text)


if __name__ == "__main__":
    main()
