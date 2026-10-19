#!/usr/bin/env python3
"""
Test 4 - Long-term memory across sessions, with proof.

  Session A: customer states a UNIQUE preference (not in any mock data).
  Poll:      waits for AgentCore to extract it, showing the stored memory record.
  Session B: new session, same customer, asks for it WITHOUT repeating the value.

  python run_memory_test.py          # run from the project folder
Log: test_logs/test4_long_term_memory.txt
"""
import json, os, random, re, subprocess, sys, time
from pathlib import Path

import boto3

sys.stdout.reconfigure(encoding="utf-8")

SRC = Path("main.py").read_text(encoding="utf-8")


def cfg(name):
    return re.search(rf'^{name}\s*=\s*"(.*?)"', SRC, re.M).group(1)


MEMORY_ID, REGION = cfg("MEMORY_ID"), cfg("REGION")
CUST = "CUST-123"
POLL_TOTAL, POLL_EVERY = 480, 20  # seconds
ENV = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "AGENTCORE_SUPPRESS_RECOMMENDATION": "1"}

color = random.choice(["tangerine", "pistachio", "cobalt", "saffron", "magenta", "turquoise"])
stamp = int(time.time())
SESSION_A, SESSION_B = f"mem-a-{stamp}", f"mem-b-{stamp}"
PROMPT_A = (f"Please remember my delivery preferences: I always want my orders left at the side gate "
            f"and gift-wrapped in {color} paper.")
PROMPT_B = ("Which gift-wrap paper color and delivery spot do I prefer for my orders? "
            "Answer from what you already know about me.")

out = []


def log(s=""):
    out.append(s)
    print(s, flush=True)


def invoke(session, prompt):
    payload = {"prompt": prompt, "customer_id": CUST, "session_id": session}
    r = subprocess.run(["agentcore", "invoke", json.dumps(payload)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=ENV)
    return payload, r.returncode, (r.stdout or "") + (r.stderr or "")


sess = boto3.Session(region_name=REGION)
control = sess.client("bedrock-agentcore-control")
data = sess.client("bedrock-agentcore")


def strategies():
    mem = control.get_memory(memoryId=MEMORY_ID)["memory"]
    result = []
    for s in mem.get("strategies") or mem.get("memoryStrategies") or []:
        sid = s.get("memoryStrategyId") or s.get("strategyId") or ""
        templates = s.get("namespaceTemplates") or s.get("namespaces") or []
        stype = s.get("memoryStrategyType") or s.get("type") or s.get("name")
        status = s.get("status")
        if templates:
            ns = templates[0].replace("{memoryStrategyId}", sid).replace("{actorId}", CUST)
            result.append((stype, status, ns))
    return result


def fetch_records(query):
    found = []
    for stype, _status, ns in strategies():
        try:
            resp = data.retrieve_memory_records(
                memoryId=MEMORY_ID, namespace=ns,
                searchCriteria={"searchQuery": query, "topK": 5},
            )
            for rec in resp.get("memoryRecordSummaries", []):
                found.append((stype, ns, rec.get("content", {}).get("text", "")))
        except Exception as e:
            log(f"   retrieve error in {ns}: {e}")
    return found


log(f"=== Test 4: long-term memory (memory {MEMORY_ID}, customer {CUST}) ===")
log(f"Unique preference value used: {color}\n")
log("Strategies / namespaces:")
for stype, status, ns in strategies():
    log(f"  - {stype} [{status}] -> {ns}")

# ── Session A ────────────────────────────────────────────────────────────────
payload, code, resp = invoke(SESSION_A, PROMPT_A)
log(f"\n--- SESSION A ({SESSION_A})\nREQUEST: {json.dumps(payload)}\nEXIT CODE: {code}\nRESPONSE:\n{resp}")

try:
    events = data.list_events(memoryId=MEMORY_ID, actorId=CUST, sessionId=SESSION_A,
                              includePayloads=True, maxResults=10).get("events", [])
    log(f"Saved events in session A: {len(events)}")
except Exception as e:
    log(f"list_events error: {e}")

# ── Wait for extraction, show the stored record ──────────────────────────────
log(f"\nWaiting for memory extraction (up to {POLL_TOTAL}s)...")
stored = []
waited = 0
while waited <= POLL_TOTAL:
    recs = fetch_records("gift-wrap paper color and delivery location preference")
    stored = [r for r in recs if color in r[2].lower()]
    if stored:
        break
    time.sleep(POLL_EVERY)
    waited += POLL_EVERY
    print(f"   ...{waited}s, {len(recs)} record(s), none with '{color}' yet", flush=True)

if stored:
    log(f"Memory record found after ~{waited}s:")
    for stype, ns, text in stored:
        log(f"  [{stype}] namespace={ns}\n     {text}")
else:
    log("NO memory record containing the preference was extracted. Records currently stored:")
    for stype, ns, text in fetch_records("preferences"):
        log(f"  [{stype}] {ns}: {text}")

# ── Session B ────────────────────────────────────────────────────────────────
payload, code, resp = invoke(SESSION_B, PROMPT_B)
log(f"\n--- SESSION B ({SESSION_B}) - new session, value NOT repeated\nREQUEST: {json.dumps(payload)}\nEXIT CODE: {code}\nRESPONSE:\n{resp}")

ok = color in resp.lower()
log(f"\nRESULT: {'PASS - session B recalled ' + color if ok else 'FAIL - ' + color + ' not found in session B response'}")
Path("test_logs").mkdir(exist_ok=True)
Path("test_logs/test4_long_term_memory.txt").write_text("\n".join(out), encoding="utf-8")
sys.exit(0 if ok else 1)
