#!/usr/bin/env python3
"""
Creates all AWS resources for the CS agent and patches main.py with the IDs.

  python setup_infra.py --region us-east-1 [--profile NAME] [--main main.py]

Creates: IAM roles, 2 Lambdas, API Gateway (order-tracker proxy), AgentCore
Gateway (+2 targets, authorizer NONE), AgentCore Memory (semantic + preference),
S3 bucket + S3 Vectors index + Bedrock Knowledge Base (catalog ingested).
Safe to re-run (reuses existing resources by name).
Writes infra_outputs.json.
"""
import argparse, io, json, re, sys, time, zipfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

ap = argparse.ArgumentParser()
ap.add_argument("--region")
ap.add_argument("--profile")
ap.add_argument("--main", default="main.py")
args = ap.parse_args()

REGION = args.region or boto3.Session(profile_name=args.profile).region_name or "us-east-1"
sess = boto3.Session(profile_name=args.profile, region_name=REGION)
ACCT = sess.client("sts").get_caller_identity()["Account"]

iam = sess.client("iam")
lam = sess.client("lambda")
apigw = sess.client("apigateway")
ctl = sess.client("bedrock-agentcore-control")
s3 = sess.client("s3")
s3v = sess.client("s3vectors")
bka = sess.client("bedrock-agent")

STAGE = "prod"
LAMBDA_ROLE = "cs-agent-lambda-role"
GATEWAY_ROLE = "cs-agent-gateway-role"
KB_ROLE = "cs-agent-kb-role"
GATEWAY_NAME = "cs-support-gateway"
MEMORY_NAME = "cs_agent_memory"
KB_NAME = "cs-agent-kb"
BUCKET = f"cs-agent-kb-{ACCT}-{REGION}"
VBUCKET = f"cs-agent-vectors-{ACCT}-{REGION}"
VINDEX = "catalog-index"
EMBED_MODEL = f"arn:aws:bedrock:{REGION}::foundation-model/amazon.titan-embed-text-v2:0"


def log(msg):
    print(f"[+] {msg}", flush=True)


def find(name):
    for d in (".", "lambda", "starter/lambda", "starter", ".."):
        f = Path(d) / name
        if f.exists():
            return f
    sys.exit(f"Missing file: {name} (run from the project folder)")


def retry(fn, codes, tries=20, delay=6):
    for i in range(tries):
        try:
            return fn()
        except ClientError as e:
            if e.response["Error"]["Code"] not in codes or i == tries - 1:
                raise
            time.sleep(delay)


def ignore(fn, codes=("ConflictException", "BucketAlreadyOwnedByYou", "ResourceConflictException")):
    try:
        return fn()
    except ClientError as e:
        if e.response["Error"]["Code"] not in codes:
            raise


def ensure_role(name, service, policy, managed=None):
    trust = {
        "Version": "2012-10-17",
        "Statement": [{"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}],
    }
    try:
        arn = iam.create_role(RoleName=name, AssumeRolePolicyDocument=json.dumps(trust))["Role"]["Arn"]
        new = True
    except iam.exceptions.EntityAlreadyExistsException:
        arn = iam.get_role(RoleName=name)["Role"]["Arn"]
        new = False
    if managed:
        iam.attach_role_policy(RoleName=name, PolicyArn=managed)
    if policy:
        iam.put_role_policy(RoleName=name, PolicyName="inline", PolicyDocument=json.dumps(policy))
    return arn, new


# ── 1. IAM roles ──────────────────────────────────────────────────────────────
log("IAM roles")
lambda_role_arn, n1 = ensure_role(
    LAMBDA_ROLE, "lambda.amazonaws.com", None,
    managed="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
)
gateway_role_arn, n2 = ensure_role(
    GATEWAY_ROLE, "bedrock-agentcore.amazonaws.com",
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "lambda:InvokeFunction",
                "Resource": [
                    f"arn:aws:lambda:{REGION}:{ACCT}:function:order-tracker",
                    f"arn:aws:lambda:{REGION}:{ACCT}:function:refund-processor",
                ],
            },
            {
                "Effect": "Allow",
                "Action": "execute-api:Invoke",
                "Resource": f"arn:aws:execute-api:{REGION}:{ACCT}:*/*/GET/*",
            },
        ],
    },
)
kb_role_arn, n3 = ensure_role(
    KB_ROLE, "bedrock.amazonaws.com",
    {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "bedrock:InvokeModel", "Resource": EMBED_MODEL},
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{BUCKET}", f"arn:aws:s3:::{BUCKET}/*"],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "s3vectors:GetIndex", "s3vectors:QueryVectors", "s3vectors:PutVectors",
                    "s3vectors:GetVectors", "s3vectors:DeleteVectors",
                ],
                "Resource": [
                    f"arn:aws:s3vectors:{REGION}:{ACCT}:bucket/{VBUCKET}",
                    f"arn:aws:s3vectors:{REGION}:{ACCT}:bucket/{VBUCKET}/index/*",
                ],
            },
        ],
    },
)
if n1 or n2 or n3:
    log("waiting for IAM propagation")
    time.sleep(15)

# ── 2. Lambdas ────────────────────────────────────────────────────────────────
def deploy_lambda(name, file):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(file, file.name)
    code = buf.getvalue()
    try:
        retry(
            lambda: lam.create_function(
                FunctionName=name, Runtime="python3.12", Role=lambda_role_arn,
                Handler=f"{file.stem}.lambda_handler", Code={"ZipFile": code}, Timeout=30,
            ),
            ("InvalidParameterValueException",),
        )
    except lam.exceptions.ResourceConflictException:
        lam.update_function_code(FunctionName=name, ZipFile=code)
    lam.get_waiter("function_updated_v2").wait(FunctionName=name)
    return lam.get_function(FunctionName=name)["Configuration"]["FunctionArn"]


log("Lambdas")
order_arn = deploy_lambda("order-tracker", find("order_tracker.py"))
refund_arn = deploy_lambda("refund-processor", find("refund_processor.py"))

# ── 3. API Gateway in front of order-tracker (proxy events) ───────────────────
log("API Gateway")
api_id = next((a["id"] for a in apigw.get_rest_apis(limit=500)["items"] if a["name"] == "order-tracker-api"), None)
if not api_id:
    api_id = apigw.create_rest_api(
        name="order-tracker-api", endpointConfiguration={"types": ["REGIONAL"]}
    )["id"]
    root = apigw.get_resources(restApiId=api_id)["items"][0]["id"]

    def res(parent, part):
        return apigw.create_resource(restApiId=api_id, parentId=parent, pathPart=part)["id"]

    orders = res(root, "orders")
    r_order = res(orders, "{order_id}")
    customers = res(root, "customers")
    r_cust = res(customers, "{customer_id}")
    r_cust_orders = res(r_cust, "orders")
    uri = f"arn:aws:apigateway:{REGION}:lambda:path/2015-03-31/functions/{order_arn}/invocations"
    for rid in (r_order, r_cust, r_cust_orders):
        apigw.put_method(restApiId=api_id, resourceId=rid, httpMethod="GET", authorizationType="AWS_IAM")
        apigw.put_integration(
            restApiId=api_id, resourceId=rid, httpMethod="GET",
            type="AWS_PROXY", integrationHttpMethod="POST", uri=uri,
        )
    ignore(lambda: lam.add_permission(
        FunctionName="order-tracker", StatementId=f"apigw-{api_id}",
        Action="lambda:InvokeFunction", Principal="apigateway.amazonaws.com",
        SourceArn=f"arn:aws:execute-api:{REGION}:{ACCT}:{api_id}/*/*/*",
    ))
    apigw.create_deployment(restApiId=api_id, stageName=STAGE)

# Gateway parses the exported OpenAPI spec: every method needs a declared response.
for r in apigw.get_resources(restApiId=api_id, limit=500, embed=["methods"])["items"]:
    if "GET" in r.get("resourceMethods", {}):
        ignore(
            lambda r=r: apigw.put_method_response(
                restApiId=api_id, resourceId=r["id"], httpMethod="GET", statusCode="200"
            ),
            ("ConflictException",),
        )
apigw.create_deployment(restApiId=api_id, stageName=STAGE)

# ── 4. AgentCore Gateway + targets ────────────────────────────────────────────
log("AgentCore Gateway")
gw_id = next((g["gatewayId"] for g in ctl.list_gateways()["items"] if g["name"] == GATEWAY_NAME), None)
if not gw_id:
    gw_id = retry(
        lambda: ctl.create_gateway(
            name=GATEWAY_NAME, roleArn=gateway_role_arn,
            protocolType="MCP", authorizerType="NONE",
        ),
        ("ValidationException", "AccessDeniedException"),
    )["gatewayId"]


def wait_ready(fn):
    while True:
        r = fn()
        st = r["status"]
        if st == "READY":
            return r
        if "FAILED" in st:
            sys.exit(f"FAILED: {json.dumps(r, default=str)[:1500]}")
        time.sleep(5)


gw = wait_ready(lambda: ctl.get_gateway(gatewayIdentifier=gw_id))
gateway_url = gw["gatewayUrl"]

existing, failed = set(), set()
for t in ctl.list_gateway_targets(gatewayIdentifier=gw_id)["items"]:
    if "FAILED" in t["status"] or "UNSUCCESSFUL" in t["status"]:
        ctl.delete_gateway_target(gatewayIdentifier=gw_id, targetId=t["targetId"])
        failed.add(t["name"])
        log(f"deleting failed target {t['name']}")
    else:
        existing.add(t["name"])
while failed & {t["name"] for t in ctl.list_gateway_targets(gatewayIdentifier=gw_id)["items"]}:
    time.sleep(3)
iam_cred = [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]

if "refund-processor" not in existing:
    log("target: refund-processor")
    schema = json.loads(find("lambda_schema").read_text(encoding="utf-8"))
    tid = ctl.create_gateway_target(
        gatewayIdentifier=gw_id, name="refund-processor",
        targetConfiguration={"mcp": {"lambda": {"lambdaArn": refund_arn, "toolSchema": {"inlinePayload": schema}}}},
        credentialProviderConfigurations=iam_cred,
    )["targetId"]
    wait_ready(lambda: ctl.get_gateway_target(gatewayIdentifier=gw_id, targetId=tid))

if "order-tracker" not in existing:
    log("target: order-tracker (API Gateway)")
    tid = ctl.create_gateway_target(
        gatewayIdentifier=gw_id, name="order-tracker",
        targetConfiguration={"mcp": {"apiGateway": {
            "restApiId": api_id, "stage": STAGE,
            "apiGatewayToolConfiguration": {
                "toolFilters": [
                    {"filterPath": "/orders/{order_id}", "methods": ["GET"]},
                    {"filterPath": "/customers/{customer_id}/orders", "methods": ["GET"]},
                    {"filterPath": "/customers/{customer_id}", "methods": ["GET"]},
                ],
                "toolOverrides": [
                    {"name": "get_order", "path": "/orders/{order_id}", "method": "GET",
                     "description": "Get a single order (status, items, tracking, delivery date) by order ID, e.g. ORD-001."},
                    {"name": "get_customer_orders", "path": "/customers/{customer_id}/orders", "method": "GET",
                     "description": "List all orders for a customer ID, e.g. CUST-123."},
                    {"name": "get_customer", "path": "/customers/{customer_id}", "method": "GET",
                     "description": "Get customer profile: name, loyalty points, tier."},
                ],
            },
        }}},
        credentialProviderConfigurations=iam_cred,
    )["targetId"]
    wait_ready(lambda: ctl.get_gateway_target(gatewayIdentifier=gw_id, targetId=tid))

# ── 5. AgentCore Memory ───────────────────────────────────────────────────────
log("AgentCore Memory")
from bedrock_agentcore.memory import MemoryClient

mc = MemoryClient(region_name=REGION)


def strategies(key):
    return [
        {"semanticMemoryStrategy": {"name": "facts", key: ["cs_agent/{actorId}/facts"]}},
        {"userPreferenceMemoryStrategy": {"name": "preferences", key: ["cs_agent/{actorId}/preferences"]}},
    ]


memory_id = next(
    (m.get("id") or m.get("memoryId") for m in mc.list_memories()
     if (m.get("id") or m.get("memoryId") or "").startswith(MEMORY_NAME + "-")),
    None,
)
if not memory_id:
    last = None
    for key in ("namespaces", "namespaceTemplates"):
        try:
            mem = mc.create_memory_and_wait(name=MEMORY_NAME, strategies=strategies(key), event_expiry_days=30)
            memory_id = mem.get("id") or mem.get("memoryId")
            break
        except Exception as e:
            last = e
    if not memory_id:
        sys.exit(f"Memory creation failed: {last}")

# ── 6. Knowledge Base ─────────────────────────────────────────────────────────
log("Knowledge Base")
if REGION == "us-east-1":
    ignore(lambda: s3.create_bucket(Bucket=BUCKET))
else:
    ignore(lambda: s3.create_bucket(Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}))
s3.upload_file(str(find("product_catalog.txt")), BUCKET, "product_catalog.txt")

ignore(lambda: s3v.create_vector_bucket(vectorBucketName=VBUCKET))
ignore(lambda: s3v.create_index(
    vectorBucketName=VBUCKET, indexName=VINDEX, dataType="float32", dimension=1024,
    distanceMetric="cosine",
    metadataConfiguration={"nonFilterableMetadataKeys": ["AMAZON_BEDROCK_TEXT"]},
))
index_arn = s3v.get_index(vectorBucketName=VBUCKET, indexName=VINDEX)["index"]["indexArn"]

kb_id = next((k["knowledgeBaseId"] for k in bka.list_knowledge_bases()["knowledgeBaseSummaries"] if k["name"] == KB_NAME), None)
if not kb_id:
    kb_id = retry(
        lambda: bka.create_knowledge_base(
            name=KB_NAME, roleArn=kb_role_arn,
            knowledgeBaseConfiguration={
                "type": "VECTOR",
                "vectorKnowledgeBaseConfiguration": {
                    "embeddingModelArn": EMBED_MODEL,
                    "embeddingModelConfiguration": {
                        "bedrockEmbeddingModelConfiguration": {"dimensions": 1024, "embeddingDataType": "FLOAT32"}
                    },
                },
            },
            storageConfiguration={"type": "S3_VECTORS", "s3VectorsConfiguration": {"indexArn": index_arn}},
        ),
        ("ValidationException", "AccessDeniedException"),
    )["knowledgeBase"]["knowledgeBaseId"]

while bka.get_knowledge_base(knowledgeBaseId=kb_id)["knowledgeBase"]["status"] != "ACTIVE":
    time.sleep(5)

ds_id = next((d["dataSourceId"] for d in bka.list_data_sources(knowledgeBaseId=kb_id)["dataSourceSummaries"]), None)
if not ds_id:
    ds_id = bka.create_data_source(
        knowledgeBaseId=kb_id, name="catalog",
        dataSourceConfiguration={"type": "S3", "s3Configuration": {"bucketArn": f"arn:aws:s3:::{BUCKET}"}},
    )["dataSource"]["dataSourceId"]

job = bka.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)["ingestionJob"]["ingestionJobId"]
while True:
    st = bka.get_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job)["ingestionJob"]
    if st["status"] == "COMPLETE":
        break
    if st["status"] == "FAILED":
        sys.exit(f"Ingestion failed: {st.get('failureReasons')}")
    time.sleep(5)

# ── 7. Patch main.py + save outputs ───────────────────────────────────────────
out = {
    "region": REGION, "account": ACCT, "gateway_url": gateway_url, "gateway_id": gw_id,
    "kb_id": kb_id, "memory_id": memory_id, "order_tracker_arn": order_arn,
    "refund_processor_arn": refund_arn, "rest_api_id": api_id,
}
Path("infra_outputs.json").write_text(json.dumps(out, indent=2))

mp = Path(args.main)
if mp.exists():
    txt = mp.read_text(encoding="utf-8")
    for name, val in (("GATEWAY_URL", gateway_url), ("KB_ID", kb_id), ("REGION", REGION), ("MEMORY_ID", memory_id)):
        txt = re.sub(rf'^({name}\s*=\s*)".*?"', lambda m, v=val: f'{m.group(1)}"{v}"', txt, flags=re.M)
    mp.write_text(txt, encoding="utf-8")
    log(f"patched {mp}")

print(json.dumps(out, indent=2))