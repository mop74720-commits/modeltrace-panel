"""Explicit API adapters; one HTTP request per probe, no hidden retries."""
import http.client
import json
import os
import socket
from urllib.parse import urlsplit

MAX_RESPONSE = 2 * 1024 * 1024
REASONING_EFFORTS = {"", "none", "minimal", "low", "medium", "high", "xhigh", "max"}


def validate_reasoning_effort(value, protocol):
    if not isinstance(value, str) or value not in REASONING_EFFORTS:
        raise ValueError("不支持的推理档位。")
    if value and protocol == "anthropic":
        raise ValueError("此推理档位仅适用于 Responses 和 Chat Completions。")
    return value


class UpstreamError(Exception):
    def __init__(self, public_message):
        super().__init__(public_message)
        self.public_message = public_message


def endpoint(base_url, protocol):
    suffixes = {"openai": "/chat/completions", "responses": "/responses", "anthropic": "/messages"}
    suffix = suffixes[protocol]
    value = base_url.rstrip("/")
    if any(value.endswith(s) for s in suffixes.values()):
        if not value.endswith(suffix):
            raise ValueError("完整接口路径与所选协议不一致。")
        return value
    return value + ("" if value.endswith("/v1") else "/v1") + suffix


def build_request(model, key, protocol, prompt, budget, temperature, token_field, reasoning_effort=""):
    validate_reasoning_effort(reasoning_effort, protocol)
    headers = {"Content-Type": "application/json", "Accept": "application/json", "Authorization": f"Bearer {key}",
               "User-Agent": os.environ.get("MODELTRACE_USER_AGENT", "ModelTrace-Panel/1.0")}
    body = {"model": model, "stream": False}
    if protocol == "responses":
        body.update(input=[{"role": "user", "content": prompt}], max_output_tokens=budget, store=False)
    else:
        body["messages"] = [{"role": "user", "content": prompt}]
        body["max_tokens" if protocol == "anthropic" else token_field] = budget
    if protocol == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    if temperature is not None:
        body["temperature"] = temperature
    if reasoning_effort:
        if protocol == "responses":
            body["reasoning"] = {"effort": reasoning_effort}
        else:
            body["reasoning_effort"] = reasoning_effort
    return body, headers


def parse_response(payload, protocol):
    if protocol == "openai":
        choice = payload["choices"][0]
        message = choice["message"]
        text = message.get("content") or ""
        if isinstance(text, list):
            text = "".join(p.get("text", "") for p in text if p.get("type") == "text")
        reason = choice.get("finish_reason", "unknown")
        complete = reason == "stop" and not message.get("refusal")
    elif protocol == "anthropic":
        text = "".join(b.get("text", "") for b in payload["content"] if b.get("type") == "text")
        reason = payload.get("stop_reason", "unknown")
        complete = reason == "end_turn"
    else:
        text = "".join(c.get("text", "") for item in payload["output"] if item.get("type") == "message"
                       for c in item.get("content", []) if c.get("type") == "output_text")
        reason = payload.get("status", "unknown")
        refused = any(c.get("type") == "refusal" for item in payload["output"] for c in item.get("content", []))
        complete = reason == "completed" and not refused
    if not isinstance(text, str):
        raise ValueError("Invalid content")
    # Only numeric usage fields are returned; no upstream metadata or headers.
    usage = {k: v for k, v in (payload.get("usage") or {}).items()
             if k in {"input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens"} and isinstance(v, (int, float))}
    return {"text": text, "complete": bool(complete), "finish_reason": str(reason)[:80], "usage": usage}


def complete(base_url, address, key, model, protocol, prompt, budget, temperature, token_field, reasoning_effort=""):
    url = urlsplit(endpoint(base_url, protocol))
    body, headers = build_request(model, key, protocol, prompt, budget, temperature, token_field, reasoning_effort)
    cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    connection = cls(url.hostname, port=url.port, timeout=240)
    # Connect to the IP already checked by the server; retain original host for TLS SNI/certificate verification.
    connection._create_connection = lambda target, timeout, source_address=None: socket.create_connection((address, target[1]), timeout, source_address)
    try:
        connection.request("POST", url.path, body=json.dumps(body).encode("utf-8"), headers=headers)
        response = connection.getresponse()
        if not 200 <= response.status < 300:
            hints = {400: "参数或模型名不被接受，请检查模型支持的推理档位、输出预算字段和 temperature。", 401: "API Key 无效或无权限。", 403: "上游拒绝访问，请检查权限或网关限制。", 404: "接口路径或模型不存在。", 429: "上游限流或配额不足。"}
            raise UpstreamError(f"上游 HTTP {response.status}：" + hints.get(response.status, "请求失败；请检查上游服务。") + " 本次未自动重试。")
        raw = response.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise UpstreamError("上游响应超过 2 MiB，已终止读取。")
        try:
            return parse_response(json.loads(raw), protocol)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError):
            raise UpstreamError("上游返回格式与所选协议不一致，或响应不是有效 JSON。") from None
    except (OSError, http.client.HTTPException):
        raise UpstreamError("连接上游失败、TLS 校验失败或请求超时（240 秒）；本次未自动重试。") from None
    finally:
        connection.close()
