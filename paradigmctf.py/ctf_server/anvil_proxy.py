import json
import logging
from ast import Dict, List
from contextlib import asynccontextmanager
from typing import Any, Optional

import aiohttp
from fastapi import FastAPI, Request

from .utils import load_database


ALLOWED_NAMESPACES = ["web3", "eth", "net"]
DISALLOWED_METHODS = [
    "eth_sign",
    "eth_signTransaction",
    "eth_signTypedData",
    "eth_signTypedData_v3",
    "eth_signTypedData_v4",
    "eth_sendTransaction",
    "eth_sendUnsignedTransaction",
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    global session, database
    session = aiohttp.ClientSession()
    database = load_database()

    yield

    await session.close()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def root():
    return "rpc proxy running"


def jsonrpc_fail(id: Any, code: int, message: str) -> Dict:
    return {
        "jsonrpc": "2.0",
        "id": id,
        "error": {
            "code": code,
            "message": message,
        },
    }


def validate_request(request: Any) -> Optional[Dict]:
    if not isinstance(request, dict):
        return jsonrpc_fail(None, -32600, "expected json object")

    request_id = request.get("id")
    request_method = request.get("method")

    if request_id is None:
        return jsonrpc_fail(None, -32600, "invalid jsonrpc id")

    if not isinstance(request_method, str):
        return jsonrpc_fail(request["id"], -32600, "invalid jsonrpc method")

    if (
        request_method.split("_")[0] not in ALLOWED_NAMESPACES
        or request_method in DISALLOWED_METHODS
    ):
        return jsonrpc_fail(request["id"], -32600, "forbidden jsonrpc method")

    return None


async def proxy_request(
    external_id: str, anvil_id: str, request_id: Optional[str], body: Any
) -> Optional[Any]:
    user_data = database.get_instance_by_external_id(external_id)
    if user_data is None:
        return jsonrpc_fail(request_id, -32602, "invalid rpc url, instance not found")

    anvil_instance = user_data.get("anvil_instances", {}).get(anvil_id, None)
    if anvil_instance is None:
        return jsonrpc_fail(request_id, -32602, "invalid rpc url, chain not found")

    instance_host = f"http://{anvil_instance['ip']}:{anvil_instance['port']}"

    try:
        async with session.post(instance_host, json=body) as resp:
            return await resp.json()
    except Exception as e:
        logging.error(
            "failed to proxy anvil request to %s/%s", external_id, anvil_id, exc_info=e
        )
        return jsonrpc_fail(request_id, -32602, str(e))


@app.post("/{external_id}/{anvil_id}")
async def rpc(external_id: str, anvil_id: str, request: Request):
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return jsonrpc_fail(None, -32600, "expected json body")

    # special handling for batch requests
    if isinstance(body, list):
        responses = []
        for idx, req in enumerate(body):
            validation_error = validate_request(req)
            responses.append(validation_error)

            if validation_error is not None:
                # neuter the request
                body[idx] = {
                    "jsonrpc": "2.0",
                    "id": idx,
                    "method": "web3_clientVersion",
                }

        upstream_responses = await proxy_request(external_id, anvil_id, None, body)

        for idx in range(len(responses)):
            if responses[idx] is None:
                if isinstance(upstream_responses, List):
                    responses[idx] = upstream_responses[idx]
                else:
                    responses[idx] = upstream_responses

        return responses

    validation_resp = validate_request(body)
    if validation_resp is not None:
        return validation_resp

    return await proxy_request(external_id, anvil_id, body["id"], body)
