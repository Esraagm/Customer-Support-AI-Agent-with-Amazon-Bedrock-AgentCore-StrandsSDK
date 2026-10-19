#!/usr/bin/env python3
"""
Calls the AgentCore Gateway MCP tools directly and logs tool name, arguments
and raw response for BOTH target types:
  - order-tracker    (API Gateway REST target)
  - refund-processor (Lambda target)

  python verify_gateway.py        # run from the project folder (reads GATEWAY_URL from main.py)
Log: test_logs/gateway_tools_verification.txt
"""
import json, re, sys
from pathlib import Path

from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client

sys.stdout.reconfigure(encoding="utf-8")

URL = re.search(r'^GATEWAY_URL\s*=\s*"(.*?)"', Path("main.py").read_text(encoding="utf-8"), re.M).group(1)

CALLS = [
    ("order-tracker (API Gateway)", "get_order", {"order_id": "ORD-001"}),
    ("order-tracker (API Gateway)", "get_customer_orders", {"customer_id": "CUST-123"}),
    ("order-tracker (API Gateway)", "get_customer", {"customer_id": "CUST-123"}),
    ("refund-processor (Lambda)", "initiate_refund", {"order_id": "ORD-002", "reason": "Item arrived damaged", "amount": 139.99}),
    ("refund-processor (Lambda)", "check_refund_status", {"refund_id": "REF-TEST0001"}),
    ("refund-processor (Lambda)", "get_return_label", {"order_id": "ORD-002"}),
]

out = [f"Gateway: {URL}\n"]


def log(s=""):
    out.append(s)
    print(s)


client = MCPClient(lambda: streamable_http_client(URL))
with client:
    page = client.list_tools_sync()
    tools = list(page)
    while getattr(page, "pagination_token", None):
        page = client.list_tools_sync(pagination_token=page.pagination_token)
        tools.extend(page)
    by_name = {t.tool_name: t for t in tools}

    log("=== Registered Gateway tools ===")
    for name, t in by_name.items():
        log(f"- {name}  schema: {json.dumps(t.tool_spec.get('inputSchema', {}).get('json', {}).get('properties', {}))}")

    failures = 0
    for i, (target, short, args) in enumerate(CALLS):
        full = next((n for n in by_name if n == short or n.endswith("___" + short)), None)
        log(f"\n=== {target} :: {short} ===")
        if not full:
            log("TOOL NOT FOUND")
            failures += 1
            continue
        log(f"tool name : {full}")
        log(f"arguments : {json.dumps(args)}")
        try:
            res = client.call_tool_sync(tool_use_id=f"verify-{i}", name=full, arguments=args)
            log(f"status    : {res.get('status')}")
            for block in res.get("content", []):
                log(f"response  : {block.get('text', json.dumps(block))}")
            if res.get("status") != "success":
                failures += 1
        except Exception as e:
            failures += 1
            log(f"ERROR     : {e}")

log(f"\nResult: {'ALL TOOL CALLS SUCCEEDED' if failures == 0 else str(failures) + ' CALL(S) FAILED'}")
Path("test_logs").mkdir(exist_ok=True)
Path("test_logs/gateway_tools_verification.txt").write_text("\n".join(out), encoding="utf-8")
