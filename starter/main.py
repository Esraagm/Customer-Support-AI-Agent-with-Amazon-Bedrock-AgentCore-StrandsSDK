"""
Customer Support AI Agent
"""

from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.memory import MemoryClient
from strands.models import BedrockModel
from strands.tools.mcp.mcp_client import MCPClient
from mcp.client.streamable_http import streamable_http_client
import argparse, json
import os, asyncio, boto3
from strands.hooks import (
    HookProvider, AfterInvocationEvent, HookRegistry, MessageAddedEvent,
)
import logging
import uuid
from typing import Dict
from bedrock_agentcore.tools.code_interpreter_client import code_session
from strands_tools.browser import AgentCoreBrowser


logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger("CSAI_Agent")
logger.setLevel(logging.INFO)

# ── TODO 1 ────────────────────────────────────────────────────────────────────
app = BedrockAgentCoreApp()

os.environ["BYPASS_TOOL_CONSENT"] = "true"

# ── TODO 2 ────────────────────────────────────────────────────────────────────
GATEWAY_URL = "https://cs-support-gateway-7kcpqxg4nj.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
KB_ID       = "PZJWBJEXOL"
REGION      = "us-east-1"
MEMORY_ID   = "cs_agent_memory-DkRmDQ3zRP"

# ── TODO 3 ────────────────────────────────────────────────────────────────────
model_id = "global.amazon.nova-2-lite-v1:0"
model = BedrockModel(model_id=model_id)
memory_client = MemoryClient(region_name=REGION)
_bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=REGION)

SYSTEM_PROMPT = (
    "You are a helpful e-commerce customer support agent. "
    "Use the order and refund tools for order tracking and refunds, "
    "search_knowledge_base for product, policy and loyalty questions, "
    "calculate_loyalty_discount for any discount math, and the browser for "
    "live web information. Never guess order data; call a tool. "
    "Use 'Customer Context' in the user message, if present, to personalise replies."
)


# ── TODO 4 ────────────────────────────────────────────────────────────────────
def get_namespaces(mem_client: MemoryClient, memory_id: str) -> Dict:
    """Return a dict mapping strategy type → namespace template string."""
    namespaces = {}
    for strategy in mem_client.get_memory_strategies(memory_id):
        templates = strategy.get("namespaceTemplates") or strategy.get("namespaces") or []
        stype = strategy.get("memoryStrategyType") or strategy.get("type")
        sid = strategy.get("memoryStrategyId") or strategy.get("strategyId") or ""
        if templates and stype:
            namespaces[stype] = templates[0].replace("{memoryStrategyId}", sid)
    logger.info(f"Memory namespaces: {namespaces}")
    return namespaces


# ── TODO 5 ────────────────────────────────────────────────────────────────────
class MemoryHook(HookProvider):
    """Long-term memory hook for the customer support agent."""

    CTX_PREFIX = "Customer Context:\n"

    def __init__(
        self,
        actor_id: str,
        session_id: str,
        memory_client: MemoryClient,
        memory_id: str,
    ):
        self.actor_id = actor_id
        self.session_id = session_id
        self.memory_id = memory_id
        self.memory_client = memory_client
        self.namespaces = get_namespaces(memory_client, memory_id)

    def retrieve_customer_context(self, event: MessageAddedEvent):
        """Retrieve relevant memories and prepend them to the user message."""
        try:
            messages = event.agent.messages
            if not messages:
                return
            msg = messages[-1]
            if msg.get("role") != "user":
                return
            content = msg.get("content") or []
            if not content or "toolResult" in content[0] or "text" not in content[0]:
                return
            query = content[0]["text"]

            found = []
            for stype, template in self.namespaces.items():
                namespace = template.replace("{actorId}", self.actor_id).replace(
                    "{sessionId}", self.session_id
                )
                try:
                    memories = self.memory_client.retrieve_memories(
                        memory_id=self.memory_id,
                        namespace=namespace,
                        query=query,
                        top_k=5,
                    )
                except Exception as e:
                    logger.warning(f"retrieve_memories failed for {stype}: {e}")
                    continue
                for m in memories or []:
                    c = m.get("content", {})
                    text = (c.get("text", "") if isinstance(c, dict) else str(c)).strip()
                    if text:
                        found.append(f"[{stype}] {text}")

            logger.info(f"Memory retrieval for actor {self.actor_id}: {len(found)} record(s)")
            if found:
                content[0]["text"] = (
                    self.CTX_PREFIX + "\n".join(found) + f"\n\n{query}"
                )
        except Exception as e:
            logger.warning(f"Memory retrieval error: {e}", exc_info=True)

    def save_support_interaction(self, event: AfterInvocationEvent):
        """Save the completed turn to memory after the agent responds."""
        try:
            customer_query = None
            agent_response = None
            for msg in reversed(event.agent.messages):
                content = msg.get("content") or []
                texts = [b["text"] for b in content if "text" in b]
                if not texts:
                    continue
                if msg["role"] == "assistant" and agent_response is None:
                    agent_response = texts[0]
                elif (
                    msg["role"] == "user"
                    and customer_query is None
                    and not any("toolResult" in b for b in content)
                ):
                    customer_query = texts[0]
                if customer_query and agent_response:
                    break

            if not (customer_query and agent_response):
                return

            # strip injected context so it isn't stored as the user's words
            if customer_query.startswith(self.CTX_PREFIX):
                customer_query = customer_query.split("\n\n")[-1]

            logger.info(f"Saving memory event for actor {self.actor_id}, session {self.session_id}")
            self.memory_client.create_event(
                memory_id=self.memory_id,
                actor_id=self.actor_id,
                session_id=self.session_id,
                messages=[(customer_query, "USER"), (agent_response, "ASSISTANT")],
            )
        except Exception as e:
            logger.warning(f"Memory save error: {e}", exc_info=True)

    def register_hooks(self, registry: HookRegistry) -> None:  # type: ignore
        """Register both memory callbacks."""
        registry.add_callback(MessageAddedEvent, self.retrieve_customer_context)
        registry.add_callback(AfterInvocationEvent, self.save_support_interaction)


# ── TODO 6 ────────────────────────────────────────────────────────────────────
@tool
def search_knowledge_base(query: str) -> str:
    """
    Search the Amazon product catalog and support knowledge base.
    Use this for product specifications, return policies, warranty
    information, loyalty program details, and order status definitions.

    Args:
        query: The question or topic to search for

    Returns:
        Relevant information retrieved from the knowledge base
    """
    if not KB_ID:
        return "Knowledge base not configured."
    try:
        resp = _bedrock_runtime.retrieve(
            knowledgeBaseId=KB_ID,
            retrievalQuery={"text": query},
        )
    except Exception as e:
        return f"Knowledge base error: {e}"
    results = resp.get("retrievalResults", [])
    if not results:
        return "No relevant information found in the knowledge base."
    return "\n---\n".join(r["content"]["text"] for r in results)


# ── TODO 7 ────────────────────────────────────────────────────────────────────
_TIER_RATES = {"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}
_EARN_RATES = {"standard": 1, "device": 2, "fresh": 5}
_LOYALTY_REQUIRED_KEYS = ("points_redeemed", "tier_discount_pct", "final_total", "remaining_points")


def _parse_sandbox_result(result: dict) -> dict:
    """Parse the JSON line printed by the sandbox program out of the result envelope."""
    if result.get("isError"):
        raise RuntimeError(f"Code Interpreter reported an error: {result}")
    texts = []
    stdout = (result.get("structuredContent") or {}).get("stdout")
    if stdout:
        texts.append(stdout)
    texts += [b["text"] for b in result.get("content", []) if isinstance(b, dict) and b.get("text")]
    for text in texts:
        for line in reversed(text.strip().splitlines()):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
    raise ValueError("No JSON result printed by the Code Interpreter")


def _loyalty_fallback(loyalty_points: int, tier: str, order_total: float, product_category: str) -> dict:
    """Tier-discount-only result with the same schema as the sandbox result.

    No points are redeemed (points_redeemed = 0). Points are still earned on the
    amount actually paid, so remaining_points = balance + points_earned.
    """
    rate = _TIER_RATES.get(tier, 0.0)
    tier_discount = round(order_total * rate, 2)
    final_total = round(order_total - tier_discount, 2)
    points_earned = int(final_total * _EARN_RATES.get(product_category, 1))
    return {
        "order_total": order_total,
        "tier": tier,
        "product_category": product_category,
        "points_redeemed": 0,
        "points_value": 0.0,
        "tier_discount_pct": round(rate * 100, 2),
        "tier_discount": tier_discount,
        "final_total": final_total,
        "total_savings": tier_discount,
        "points_earned": points_earned,
        "remaining_points": loyalty_points + points_earned,
        "fallback": True,
        "note": "Code Interpreter unavailable; tier discount only, no points redeemed.",
    }


@tool
def calculate_loyalty_discount(
    loyalty_points: int,
    tier: str,
    order_total: float,
    product_category: str = "standard",
) -> str:
    """
    Calculate the loyalty discount for a customer order using the
    AgentCore Code Interpreter. Runs exact arithmetic in a secure sandbox.

    Args:
        loyalty_points:   Customer's current points balance
        tier:             Customer tier — Silver, Gold, or Platinum
        order_total:      Order total in USD
        product_category: standard, device, or fresh

    Returns:
        JSON with points_redeemed, tier_discount_pct (percent), tier_discount,
        final_total, total_savings, points_earned and remaining_points
    """
    code = f"""
import json

loyalty_points = {int(loyalty_points)}
tier = {tier!r}
order_total = {float(order_total)!r}
product_category = {product_category!r}

earn_rates = {{"standard": 1, "device": 2, "fresh": 5}}
tier_rates = {{"Silver": 0.00, "Gold": 0.10, "Platinum": 0.15}}

# 100 points = $1, min redemption 500, floor to nearest 500, cap at 50% of order
max_points_by_order = int(order_total * 0.5 * 100)
usable = min(loyalty_points, max_points_by_order)
points_redeemed = (usable // 500) * 500
points_value = points_redeemed / 100

subtotal_after_points = order_total - points_value
tier_rate = tier_rates.get(tier, 0.0)
tier_discount = round(subtotal_after_points * tier_rate, 2)
final_total = round(subtotal_after_points - tier_discount, 2)
total_savings = round(points_value + tier_discount, 2)
points_earned = int(final_total * earn_rates.get(product_category, 1))
remaining_points = loyalty_points - points_redeemed + points_earned

print(json.dumps({{
    "order_total": order_total,
    "tier": tier,
    "product_category": product_category,
    "points_redeemed": points_redeemed,
    "points_value": points_value,
    "tier_discount_pct": round(tier_rate * 100, 2),
    "tier_discount": tier_discount,
    "final_total": final_total,
    "total_savings": total_savings,
    "points_earned": points_earned,
    "remaining_points": remaining_points,
    "fallback": False,
}}))
"""
    try:
        with code_session(REGION) as ci:
            response = ci.invoke(
                "executeCode",
                {"code": code, "language": "python", "clearContext": True},
            )
            for event in response["stream"]:
                data = _parse_sandbox_result(event["result"])
                missing = [k for k in _LOYALTY_REQUIRED_KEYS if k not in data]
                if missing:
                    raise ValueError(f"Sandbox result missing keys: {missing}")
                return json.dumps(data)
        raise RuntimeError("Code Interpreter returned no result events")
    except Exception as e:
        logger.warning(f"Code Interpreter unavailable, using tier-only fallback: {e}")
        return json.dumps(_loyalty_fallback(loyalty_points, tier, float(order_total), product_category))


# ── TODO 8 ────────────────────────────────────────────────────────────────────
@app.entrypoint
async def invoke(payload, context=None):
    """
    Main handler called by AgentCore for every incoming request.

    Expected payload keys:
      prompt      (str, required) — the customer's message
      customer_id (str, optional) — unique customer identifier
      session_id  (str, optional) — session identifier; generated if absent
    """
    try:
        user_input = payload.get("prompt", "")
        actor_id = payload.get("customer_id", "guest")
        session_id = payload.get("session_id") or str(uuid.uuid4())

        memory_hook = MemoryHook(actor_id, session_id, memory_client, MEMORY_ID)

        agent_core_browser = AgentCoreBrowser(region=REGION)
        tools = [
            search_knowledge_base,
            calculate_loyalty_discount,
            agent_core_browser.browser,
        ]

        mcp_client = MCPClient(lambda: streamable_http_client(GATEWAY_URL))
        with mcp_client:
            tools.extend(mcp_client.list_tools_sync())
            agent = Agent(
                model=model,
                tools=tools,
                hooks=[memory_hook],
                system_prompt=f"{SYSTEM_PROMPT}\nCurrent customer_id: {actor_id}",
            )
            result = agent(user_input)

        return result.message["content"][0]["text"]
    except Exception as e:
        logger.exception("Agent invocation failed")
        return f"Sorry, something went wrong: {e}"


# ── CLI entry point (do not modify) ──────────────────────────────────────────
def main():
    """Run one invocation from the command line for local testing."""
    parser = argparse.ArgumentParser()
    parser.add_argument("payload", type=str)
    args = parser.parse_args()
    response = asyncio.run(invoke(json.loads(args.payload)))
    print(response)


if __name__ == "__main__":
    app.run()
    # Uncomment the line below and comment app.run() for local CLI testing:
    # main()
