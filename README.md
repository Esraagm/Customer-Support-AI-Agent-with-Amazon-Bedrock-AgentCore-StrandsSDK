# Customer Support AI Agent (Amazon Bedrock AgentCore + Strands SDK)

A conversational e-commerce support agent deployed to **Amazon Bedrock AgentCore Runtime**. One agent handles order tracking, refunds, product/policy questions, loyalty discounts, and live web lookups, and remembers customers across sessions.

## Capabilities

| Capability | How it works |
|---|---|
| Order tracking | AgentCore Gateway (MCP) → API Gateway → `order-tracker` Lambda |
| Refunds and return labels | AgentCore Gateway (MCP) → `refund-processor` Lambda (direct Lambda target) |
| Product and policy Q&A (RAG) | `search_knowledge_base` tool → Bedrock Knowledge Base (S3 Vectors) |
| Cross-session memory | `MemoryHook` + AgentCore Memory (semantic + user-preference strategies) |
| Loyalty discount | `calculate_loyalty_discount` → AgentCore Code Interpreter (sandboxed Python), with tier-only fallback |
| Live web browsing | AgentCore Browser tool (`AgentCoreBrowser`) |

Model: `global.amazon.nova-2-lite-v1:0`. Region: `us-east-1`.

## Architecture

```
Customer ─► AgentCore Runtime (main.py, Strands Agent)
              ├─ MemoryHook ───────────► AgentCore Memory
              ├─ search_knowledge_base ► Bedrock Knowledge Base ─► S3 Vectors
              ├─ calculate_loyalty_discount ► Code Interpreter sandbox
              ├─ AgentCoreBrowser ─────► Browser tool
              └─ MCPClient ► AgentCore Gateway
                               ├─ order-tracker (API Gateway ► Lambda)
                               └─ refund-processor (Lambda)
```

## Files

### Agent
| File | Purpose |
|---|---|
| `main.py` | The agent. `BedrockAgentCoreApp` entrypoint, `MemoryHook`, knowledge-base tool, loyalty-discount tool, Gateway MCP client, browser tool. Resource IDs (`GATEWAY_URL`, `KB_ID`, `REGION`, `MEMORY_ID`) are set at the top. |
| `requirements.txt` | Runtime dependencies installed in the container image. |
| `Dockerfile` | ARM64 container image (Python 3.13, `uv`), installs `requirements.txt`. Lives at `.bedrock_agentcore/<agent>/Dockerfile`. |
| `pyproject.toml` | Project metadata and local dependencies. |
| `.bedrock_agentcore.yaml` | Deployment config written by `agentcore configure` (agent name, role, ECR repo, `container_runtime: docker`). |

### Backend
| File | Purpose |
|---|---|
| `lambda/order_tracker.py` | Mock order and customer lookup (`ORD-001`..`003`, `CUST-123`, `CUST-456`). Reads API Gateway proxy events. |
| `lambda/refund_processor.py` | `initiate_refund`, `check_refund_status`, `get_return_label`. Reads the tool name from the Gateway client context. |
| `lambda_schema` | MCP tool schema for the refund Lambda target. |
| `product_catalog.txt` | Product, return-policy, and loyalty data ingested into the Knowledge Base. |

### Setup and deployment
| File | Purpose |
|---|---|
| `setup_infra.py` | Creates everything: IAM roles, both Lambdas, API Gateway, AgentCore Gateway and targets, Memory, Knowledge Base. Patches the IDs into `main.py`. Re-runnable. |
| `infra_outputs.json` | IDs and ARNs produced by `setup_infra.py`. |
| `setup_permissions.py` | After deploy, grants the runtime role access to the KB, Memory, and Browser. |

### Testing
| File | Purpose |
|---|---|
| `run_tests.py` | Runs the six required test conversations via `agentcore invoke` and saves logs (UTF-8 forced, exit codes recorded). |
| `run_memory_test.py` | Test 4. Saves a unique preference in session A, waits for extraction, shows the stored record, then checks recall in session B. |
| `verify_gateway.py` | Calls the Gateway tools directly and logs tool name, arguments, and raw response for both target types. |
| `test_loyalty_tool.py` | Offline check of the discount tool: sandbox success path and forced-failure fallback. |
| `test_logs/` | Output logs for all tests. |
| `reflection.md` | Written reflection: design decision, challenge, production consideration. |

## How to run

Prerequisites: AWS credentials configured, Docker Desktop running (ARM64 emulation enabled), Python venv with the project dependencies.

```powershell
# 1. Create AWS resources and patch IDs into main.py
python setup_infra.py --region us-east-1

# 2. Configure and deploy (local build; CodeBuild may be blocked in some lab accounts)
agentcore configure -e main.py --disable-memory
#   then make sure .bedrock_agentcore.yaml has "container_runtime: docker"
#   and copy Dockerfile to .bedrock_agentcore\<agent>\Dockerfile
agentcore deploy --local-build

# 3. Grant runtime permissions
python setup_permissions.py

# 4. Verify and test
python verify_gateway.py       # Gateway tools, both target types
python test_loyalty_tool.py    # discount tool, success and fallback
python run_tests.py            # all six tests (test 4 runs run_memory_test.py)
python run_tests.py 1 2        # or selected tests
```

Invoke manually:

```powershell
agentcore invoke '{\"prompt\": \"Where is order ORD-001?\", \"customer_id\": \"CUST-123\", \"session_id\": \"s1\"}'
```

## Tests

| # | Scenario | Expected |
|---|---|---|
| 1 | Order tracking | ORD-001 status, tracking number, carrier, delivery date |
| 2 | Refund | Refund ID, amount, status, return label URL |
| 3 | Knowledge base (RAG) | Electronics 15-day window, card refund 3-5 business days |
| 4 | Memory | Session B recalls the unique preference from session A |
| 5 | Loyalty discount | Gold, 4,250 points, $200 device order: 4,000 points redeemed, 10%, $144.00, 538 remaining points |
| 6 | Browser | Reads the heading from a live page |

Discount rules: 100 points = $1, redemption in 500-point steps capped at 50% of the order, then the tier discount (Silver 0%, Gold 10%, Platinum 15%) applies to the remaining subtotal. Points earned: 1/2/5 per $ for standard/device/fresh.

## Notes

- The Gateway uses the **NONE** authorizer, which is fine for this project but not for production. Use OAuth or IAM authorization and add ownership checks before refunds.
- Memory extraction is asynchronous and can take a few minutes after a conversation.
- On Windows, the test scripts force UTF-8 so the CLI can print emoji.

## Cleanup

```powershell
agentcore destroy
```

Then delete the Gateway, Memory, Knowledge Base, S3 buckets (catalog and vectors), API Gateway, Lambdas, and IAM roles in the console to avoid charges.
