#!/usr/bin/env python3
"""
Render the OpenAPI document from the route table.

Two outputs, and the split is the point:

* ``agents.json`` — a clean, provider-neutral OpenAPI 3.0.1 document. This is the
  committed artifact of record: it generates clients, drives contract tests and
  documents the surface. No AWS in it.
* ``agents.aws.json`` — the same document with ``x-amazon-apigateway-integration``
  injected per operation, which is what API Gateway imports. Built at deploy
  time, never committed.

**3.0.1, not 3.1.** Both dialects we mirror publish 3.1, but API Gateway's REST
API import accepts 2.0 and 3.0.x only, so the document that has to be importable
is 3.0.

Usage::

    python3 openapi/build.py                      # write openapi/agents.json
    python3 openapi/build.py --check              # fail if the committed file drifted
    python3 openapi/build.py --aws \\
        --region us-west-1 --lambda-arn arn:aws:lambda:...:function:x \\
        --out /tmp/agents.aws.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import routes as R  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
NEUTRAL = HERE / "agents.json"

VERSION = "0.1.0"

#: The request authorizer's name in the AWS variant, and how long its answer for
#: one token is cached.
AUTHORIZER = "YaitToken"
AUTHORIZER_TTL_S = 300


def _stub_response() -> dict:
    return {
        "description": "Stub acknowledgement. The skeleton answers every route so "
                       "routing, the dialect prefix and the stage can be verified "
                       "before any behaviour exists.",
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/Stub"}
            }
        },
    }


def _operation(dialect: str, method: str, path: str, op_id: str, summary: str) -> dict:
    op: dict = {
        "operationId": op_id,
        "summary": summary,
        "tags": [dialect],
        "responses": {
            "200": _stub_response(),
            "401": {"$ref": "#/components/responses/Unauthorized"},
        },
    }
    params = [
        {
            "name": name,
            "in": "path",
            "required": True,
            "schema": {"type": "string"},
        }
        for name in R.path_params(path)
    ]
    if params:
        op["parameters"] = params
    if method in ("POST", "PUT", "PATCH"):
        op["requestBody"] = {
            "required": False,
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    if op_id in R.NOT_SERVABLE_BY_APIGW:
        op["description"] = (
            "Declared for surface completeness. A real implementation cannot run "
            "through API Gateway, which buffers responses and times an integration "
            "out at about 30 s; it needs a Lambda Function URL in RESPONSE_STREAM "
            "mode behind the same distribution."
        )
    return op


def document() -> dict:
    paths: dict[str, dict] = {}
    for dialect, method, path, op_id, summary in R.ROUTES:
        full = R.full_path(dialect, path)
        paths.setdefault(full, {})[method.lower()] = _operation(
            dialect, method, path, op_id, summary
        )

    return {
        "openapi": "3.0.1",
        "info": {
            "title": "yait agents",
            "version": VERSION,
            "description": (
                "Cloud agents with the API shape of Claude Managed Agents and of "
                "OpenAI's Agents API, on any model, in your own AWS account.\n\n"
                "Two dialects behind two path prefixes, because 29 method+path "
                "pairs collide between them. A client sets base_url to "
                "`<host>/anthropic` for the Anthropic SDK, or "
                "`<host>/openai/v1` for the OpenAI SDK — the asymmetry is real: "
                "the Anthropic SDK's own paths already begin with /v1.\n\n"
                "This revision is a skeleton: every route answers a stub so that "
                "routing, dialect selection and the stage can be verified before "
                "any behaviour exists."
            ),
        },
        "tags": [
            {"name": "anthropic", "description": "Anthropic Managed Agents dialect"},
            {"name": "openai", "description": "OpenAI Agents API dialect"},
        ],
        "paths": paths,
        "components": {
            "securitySchemes": {
                # Both conventions are accepted: the Anthropic SDK sends
                # x-api-key, the OpenAI SDK sends Authorization: Bearer.
                "AnthropicApiKey": {"type": "apiKey", "in": "header", "name": "x-api-key"},
                "BearerAuth": {"type": "http", "scheme": "bearer"},
            },
            "schemas": {
                "Stub": {
                    "type": "object",
                    "required": ["ok", "dialect", "operation_id", "method", "path"],
                    "properties": {
                        "ok": {"type": "boolean"},
                        "dialect": {"type": "string", "enum": sorted(R.DIALECTS)},
                        "operation_id": {"type": "string"},
                        "method": {"type": "string"},
                        "path": {"type": "string"},
                        "path_parameters": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                        },
                        "stub": {"type": "boolean"},
                        "version": {"type": "string"},
                    },
                },
                "Error": {
                    "type": "object",
                    "required": ["type", "error"],
                    "properties": {
                        "type": {"type": "string"},
                        "error": {
                            "type": "object",
                            "required": ["type", "message"],
                            "properties": {
                                "type": {"type": "string"},
                                "message": {"type": "string"},
                            },
                        },
                    },
                },
            },
            "responses": {
                "Unauthorized": {
                    "description": "No accepted credential on the request.",
                    "content": {
                        "application/json": {"schema": {"$ref": "#/components/schemas/Error"}}
                    },
                }
            },
        },
    }


#: The function's logical id in infra/template.json. With --cfn the integration
#: uri is left as an intrinsic so CloudFormation resolves it, which avoids the
#: circle of needing the function's ARN to build the template that creates it.
CFN_FUNCTION_LOGICAL_ID = "ApiFunction"


def with_aws(doc: dict, *, region: str | None = None, lambda_arn: str | None = None,
             cfn: bool = False, api_name: str | None = None) -> dict:
    """
    Inject the proxy integration API Gateway needs, per operation.

    `api_name` also sets `info.title`, because that is where API Gateway takes the
    API's name from — on import and on every overwrite, ignoring anything passed
    alongside. Renaming afterwards does not survive the next release: a
    `put-rest-api --mode overwrite` replaces the definition including the name, so
    the name has to be in the document that gets imported. The committed document
    keeps a stable title so it does not drift per deployment.
    """
    if cfn:
        uri = {
            "Fn::Sub": (
                "arn:aws:apigateway:${AWS::Region}:lambda:path/2015-03-31/functions/"
                "${%s.Arn}/invocations" % CFN_FUNCTION_LOGICAL_ID
            )
        }
    else:
        uri = (
            f"arn:aws:apigateway:{region}:lambda:path/2015-03-31/functions/"
            f"{lambda_arn}/invocations"
        )
    integration = {
        "type": "aws_proxy",
        # Always POST: this is the gateway-to-Lambda hop, not the client's method.
        "httpMethod": "POST",
        "uri": uri,
        # No `payloadFormatVersion` here. That key belongs to HTTP APIs (v2); a
        # REST API import does not take it, and the handler reads the v1 proxy
        # shape (`httpMethod`, `path`).
    }
    out = json.loads(json.dumps(doc))
    if api_name:
        out["info"]["title"] = api_name
    # The installation token, checked by the gateway before the function runs —
    # by the same function, called as a request authorizer (tokens.authorize). The
    # answer is cached per token for five minutes, so a client's steady traffic
    # costs one check each time the cache turns over, and a request with no token
    # is refused by the gateway without calling anything.
    out["components"]["securitySchemes"][AUTHORIZER] = {
        "type": "apiKey", "in": "header", "name": "X-Yait-Key",
        "x-amazon-apigateway-authtype": "custom",
        "x-amazon-apigateway-authorizer": {
            "type": "request",
            "identitySource": "method.request.header.X-Yait-Key",
            "authorizerUri": uri,
            "authorizerResultTtlInSeconds": AUTHORIZER_TTL_S,
        },
    }
    for item in out["paths"].values():
        for op in item.values():
            op["x-amazon-apigateway-integration"] = dict(integration)
            op["security"] = [{AUTHORIZER: []}]

    # A catch-all, in the AWS variant only.
    #
    # API Gateway answers an undeclared path with its own 403 "Missing
    # Authentication Token", which is neither our error shape nor the 404 the
    # local server gives — the same request would get different answers in the
    # two environments, which is exactly the parity the product is defined by.
    # Routing everything else to the same function lets our own matcher produce
    # the 404, because an unknown path still matches no route in the table.
    out["paths"]["/{proxy+}"] = {
        "x-amazon-apigateway-any-method": {
            "parameters": [{
                "name": "proxy", "in": "path", "required": True,
                "schema": {"type": "string"},
            }],
            "responses": {},
            "x-amazon-apigateway-integration": dict(integration),
            "security": [{AUTHORIZER: []}],
        }
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit non-zero if the committed document is out of date")
    ap.add_argument("--aws", action="store_true", help="emit the API Gateway variant")
    ap.add_argument("--cfn", action="store_true",
                    help="with --aws, leave the integration uri as a CloudFormation "
                         "intrinsic instead of a resolved ARN")
    ap.add_argument("--region")
    ap.add_argument("--lambda-arn")
    ap.add_argument("--out")
    a = ap.parse_args()

    doc = document()

    if a.aws:
        if a.cfn:
            doc = with_aws(doc, cfn=True)
        elif a.region and a.lambda_arn:
            doc = with_aws(doc, region=a.region, lambda_arn=a.lambda_arn)
        else:
            ap.error("--aws needs either --cfn, or --region and --lambda-arn")

    text = json.dumps(doc, indent=2, ensure_ascii=False) + "\n"

    if a.check:
        current = NEUTRAL.read_text() if NEUTRAL.exists() else ""
        if current != text:
            print("openapi/agents.json is out of date — run python3 openapi/build.py",
                  file=sys.stderr)
            return 1
        print(f"openapi/agents.json is current ({len(doc['paths'])} paths)")
        return 0

    out = pathlib.Path(a.out) if a.out else NEUTRAL
    out.write_text(text)
    ops = sum(len(v) for v in doc["paths"].values())
    print(f"wrote {out} — {len(doc['paths'])} paths, {ops} operations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
