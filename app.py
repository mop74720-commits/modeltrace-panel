"""Self-hosted ModelTrace panel. Upstream fingerprint algorithm is vendored unchanged."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import math
import os
import secrets
import socket
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from flask import Flask, jsonify, request, send_from_directory
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from cryptography.fernet import Fernet, InvalidToken

from transport import UpstreamError, complete, validate_reasoning_effort
from vendor.fingerprint import analyze_global_outputs, generate_challenges, load_bank, parse_numbers

ROOT = Path(__file__).resolve().parent
REVISION = "60949ef522a84f66b1236b459308b48028d36949"
BANK_PATH = ROOT / "data/unified_bank.json"
BANK = load_bank(BANK_PATH)
BANK_SHA = hashlib.sha256(BANK_PATH.read_bytes()).hexdigest()
app = Flask(__name__, static_folder="static")
app.config["MAX_CONTENT_LENGTH"] = 512 * 1024
signer = URLSafeTimedSerializer(secrets.token_hex(32), salt="modeltrace-challenge")
slots = threading.BoundedSemaphore(2)
CONFIG_DIR = ROOT / "config"
CONFIG_FILE = CONFIG_DIR / "upstreams.enc"
CONFIG_KEY_FILE = CONFIG_DIR / ".key"
config_lock = threading.RLock()


class InputError(ValueError):
    """Safe validation message written by this application."""


def _config_cipher():
    configured = os.environ.get("CONFIG_ENCRYPTION_KEY", "").strip()
    if configured:
        try:
            return Fernet(configured.encode())
        except (ValueError, TypeError):
            raise RuntimeError("CONFIG_ENCRYPTION_KEY 不是有效的 Fernet 密钥。") from None
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_KEY_FILE.exists():
        CONFIG_KEY_FILE.write_bytes(Fernet.generate_key())
        try:
            os.chmod(CONFIG_KEY_FILE, 0o600)
        except OSError:
            pass
    return Fernet(CONFIG_KEY_FILE.read_bytes().strip())


def load_upstreams():
    if not CONFIG_FILE.exists():
        return {}
    try:
        raw = _config_cipher().decrypt(CONFIG_FILE.read_bytes(), ttl=None)
        payload = json.loads(raw.decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (InvalidToken, OSError, ValueError, TypeError):
        raise RuntimeError("上游配置无法解密；请检查 CONFIG_ENCRYPTION_KEY 或配置目录。") from None


def save_upstreams(configs):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    encrypted = _config_cipher().encrypt(json.dumps(configs, ensure_ascii=False).encode("utf-8"))
    temporary = CONFIG_FILE.with_suffix(".tmp")
    temporary.write_bytes(encrypted)
    temporary.replace(CONFIG_FILE)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except OSError:
        pass


def public_upstream(name, config):
    return {
        "id": name,
        "name": config.get("name", name),
        "base_url": config.get("base_url", ""),
        "model": config.get("model", ""),
        "protocol": config.get("protocol", "openai"),
        "token_field": config.get("token_field", "max_completion_tokens"),
        "has_key": bool(config.get("api_key")),
        "key_hint": "已保存" if config.get("api_key") else "未设置",
        "max_tokens": config.get("max_tokens", 4096),
        "temperature": config.get("temperature", ""),
        "reasoning_effort": config.get("reasoning_effort", ""),
    }


def failure(message, status=400):
    return jsonify(error=message), status


@app.before_request
def guard():
    if not request.path.startswith("/api/"):
        return None
    # Browser cross-origin calls must never spend a configured upstream key.
    origin = request.headers.get("Origin")
    if origin:
        parsed_origin = urlsplit(origin)
        if parsed_origin.scheme not in {"http", "https"} or parsed_origin.netloc != request.host:
            return failure("不接受跨站请求。", 403)
    access = os.environ.get("PANEL_ACCESS_TOKEN", "")
    if access and not hmac.compare_digest(request.headers.get("X-Panel-Token", "").encode(), access.encode()):
        return failure("请输入正确的面板访问口令。", 401)
    if request.method == "POST" and not request.is_json:
        return failure("请求必须为 JSON。", 415)


@app.after_request
def headers(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


@app.errorhandler(413)
def too_large(_):
    return failure("请求内容过大。", 413)


@app.errorhandler(400)
def bad_request(_):
    return failure("请求内容无效。")


@app.errorhandler(InputError)
def input_error(error):
    return failure(str(error))


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/healthz")
def health():
    return jsonify(status="ok")


@app.get("/api/info")
def info():
    upstreams = load_upstreams()
    return jsonify(
        models=[{"id": m["id"], "name": m["display_name"], "family": m.get("family_name", m.get("family", ""))} for m in BANK["models"]],
        responses=sum(m["response_count"] for m in BANK["models"]),
        revision=REVISION, bank_sha256=BANK_SHA,
        configured_key=bool(os.environ.get("UPSTREAM_API_KEY")),
        defaults={"base_url": os.environ.get("UPSTREAM_BASE_URL", ""), "model": os.environ.get("UPSTREAM_MODEL", "")},
        upstreams=[public_upstream(name, config) for name, config in upstreams.items()],
    )


@app.get("/api/upstreams")
def upstream_list():
    configs = load_upstreams()
    return jsonify(upstreams=[public_upstream(name, config) for name, config in configs.items()])


@app.post("/api/upstreams")
def upstream_save():
    with config_lock:
        return _upstream_save()


def _upstream_save():
    body = object_body()
    for field in ("name", "base_url", "model", "api_key", "protocol", "token_field", "id"):
        if field in body and not isinstance(body[field], str):
            raise InputError("配置字段类型无效。")
    base_url = normalized_url(body.get("base_url", ""))
    name = body.get("name", "").strip() or urlsplit(base_url).hostname
    if not name or len(name) > 80:
        raise InputError("配置名称不能为空且不能超过 80 个字符。")
    model = body.get("model", "").strip()
    key = body.get("api_key", "").strip()
    protocol = body.get("protocol", "openai")
    if not model or len(model) > 200:
        raise InputError("模型名无效。")
    if protocol not in {"openai", "responses", "anthropic"}:
        raise InputError("不支持的接口协议。")
    configs = load_upstreams()
    config_id = body.get("id")
    if config_id and config_id not in configs:
        raise InputError("配置不存在，请重新加载。")
    if not config_id:
        config_id = next((identifier for identifier, item in configs.items()
                          if item.get("base_url") == base_url and item.get("model") == model
                          and item.get("protocol") == protocol and item.get("name") == name), None)
    previous = configs.get(config_id, {})
    try:
        effort = validate_reasoning_effort(body.get("reasoning_effort", previous.get("reasoning_effort", "")), protocol)
    except ValueError as error:
        raise InputError(str(error)) from None
    if not key:
        if previous and previous.get("base_url") != base_url:
            raise InputError("更换上游地址时需填写新地址的 Key；原配置已保留。")
        key = previous.get("api_key", "")
    if not key or len(key) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in key):
        raise InputError("首次保存请填写 API Key；已有配置留空会保留原 Key。")
    try:
        budget = int(body.get("max_tokens", previous.get("max_tokens", 4096)))
        temperature = body.get("temperature", previous.get("temperature", ""))
        temperature = "" if temperature in (None, "") else float(temperature)
        if not 1024 <= budget <= 32768 or (temperature != "" and
                (not math.isfinite(temperature) or not 0 <= temperature <= (1 if protocol == "anthropic" else 2))):
            raise ValueError()
    except (ValueError, TypeError, OverflowError):
        raise InputError("输出预算或 Temperature 无效。") from None
    token_field = body.get("token_field", "max_completion_tokens")
    if token_field not in {"max_tokens", "max_completion_tokens"}:
        raise InputError("输出预算字段无效。")
    config_id = config_id or secrets.token_urlsafe(9)
    if config_id not in configs and len(configs) >= 50:
        raise InputError("最多保存 50 个上游配置。")
    configs[config_id] = {"name": name, "base_url": base_url, "model": model, "api_key": key,
                          "protocol": protocol, "token_field": token_field,
                          "max_tokens": budget, "temperature": temperature, "reasoning_effort": effort}
    save_upstreams(configs)
    return jsonify(upstream=public_upstream(config_id, configs[config_id]))


@app.delete("/api/upstreams/<config_id>")
def upstream_delete(config_id):
    with config_lock:
        configs = load_upstreams()
        if config_id not in configs:
            return failure("配置不存在。", 404)
        del configs[config_id]
        save_upstreams(configs)
    return jsonify(ok=True)


@app.post("/api/challenges")
def challenges():
    return jsonify(challenges=[{**c, "token": signer.dumps(c)} for c in generate_challenges(3)])


def challenge_from(token):
    if not isinstance(token, str) or len(token) > 12000:
        raise InputError("挑战标识无效。")
    try:
        return signer.loads(token, max_age=86400)
    except (BadSignature, SignatureExpired):
        raise InputError("挑战已失效，请重新生成；服务重启后旧挑战也会失效。") from None


def object_body():
    body = request.get_json()
    if not isinstance(body, dict):
        raise InputError("请求必须为 JSON 对象。")
    return body


def normalized_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise InputError("Base URL 无效。")
    if any(ord(c) < 32 or ord(c) > 126 for c in value):
        raise InputError("URL 只能包含 ASCII 字符，请对路径编码并使用 punycode 域名。")
    parsed = urlsplit(value.strip().rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise InputError("请填写 http(s) API 地址，不要在 URL 中携带凭据、查询参数或片段。")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def validate_url(value):
    value = normalized_url(value)
    parsed = urlsplit(value)
    host = parsed.hostname.lower()
    allowed = {x.strip().lower() for x in os.environ.get("UPSTREAM_HOSTS", "").split(",") if x.strip()}
    if allowed and host not in allowed:
        raise InputError("此上游域名不在服务器 UPSTREAM_HOSTS 白名单中。")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        addresses = list(dict.fromkeys(a[4][0] for a in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)))
    except (OSError, ValueError):
        raise InputError("上游域名无法解析或端口无效。") from None
    private_allowed = os.environ.get("ALLOW_PRIVATE_UPSTREAM") == "1" and host in allowed
    if not addresses or (not private_allowed and any(not ipaddress.ip_address(a).is_global for a in addresses)):
        raise InputError("默认不允许访问内网地址；自托管内网上游需由管理员显式配置域名白名单和 ALLOW_PRIVATE_UPSTREAM=1。")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), addresses[0]


@app.post("/api/probe")
def probe():
    acquired = False
    try:
        body = object_body()
        challenge = challenge_from(body.get("token"))
        saved = None
        config_id = body.get("upstream_id")
        if config_id:
            saved = load_upstreams().get(config_id)
            if not saved:
                raise InputError("所选上游配置不存在。")
        base_url, address = validate_url((saved or {}).get("base_url") or body.get("base_url") or os.environ.get("UPSTREAM_BASE_URL", ""))
        if not saved and not body.get("api_key") and base_url != os.environ.get("UPSTREAM_BASE_URL", "").strip().rstrip("/"):
            raise InputError("服务器密钥只能用于配置的 UPSTREAM_BASE_URL。")
        key = (saved or {}).get("api_key") or body.get("api_key") or os.environ.get("UPSTREAM_API_KEY", "")
        model = (saved or {}).get("model") or body.get("model") or os.environ.get("UPSTREAM_MODEL", "")
        if not isinstance(key, str) or not key.strip() or len(key) > 4096 or any(ord(c) < 32 or ord(c) > 126 for c in key):
            raise InputError("请填写有效 API Key，或在服务器配置 UPSTREAM_API_KEY。")
        if not isinstance(model, str) or not model.strip() or len(model) > 200:
            raise InputError("请填写实际请求的模型名。")
        protocol = (saved or {}).get("protocol") or body.get("protocol", "openai")
        if protocol not in {"openai", "responses", "anthropic"}:
            raise InputError("不支持的接口协议。")
        try:
            effort = validate_reasoning_effort(body.get("reasoning_effort", (saved or {}).get("reasoning_effort", "")), protocol)
        except ValueError as error:
            raise InputError(str(error)) from None
        budget = int(body.get("max_tokens", 4096))
        if not 1024 <= budget <= 32768:
            raise InputError("输出预算必须介于 1024 和 32768。")
        temp = body.get("temperature")
        temp = None if temp in (None, "") else float(temp)
        if temp is not None and (not math.isfinite(temp) or not 0 <= temp <= (1 if protocol == "anthropic" else 2)):
            raise InputError("temperature 超出该协议支持范围。")
        token_field = (saved or {}).get("token_field") or body.get("token_field", "max_completion_tokens")
        if token_field not in {"max_tokens", "max_completion_tokens"}:
            raise InputError("输出预算字段无效。")
        acquired = slots.acquire(blocking=False)
        if not acquired:
            return failure("服务器已有两个请求执行中，请稍后重试。", 429)
        start = time.monotonic()
        result = complete(base_url, address, key.strip(), model.strip(), protocol, challenge["prompt"], budget, temp, token_field, reasoning_effort=effort)
        # Do not persist or return credentials, even if an upstream echoes them.
        result["text"] = result["text"].replace(key.strip(), "[REDACTED]")
        result["finish_reason"] = result["finish_reason"].replace(key.strip(), "[REDACTED]")
        count = len(parse_numbers(result["text"]))
        minimum = max(80, math.ceil(challenge["expected_count"] * 0.55))
        accepted = result["complete"] and count >= minimum
        return jsonify(**result, accepted=accepted, parsed_numbers=count, minimum_numbers=minimum, requested_reasoning_effort=effort,
                       elapsed_seconds=round(time.monotonic() - start, 2),
                       rejection=None if accepted else ("回答被截断、拒绝或未正常完成。" if not result["complete"] else f"有效数字不足：{count}/{minimum}"))
    except InputError as error:
        return failure(str(error))
    except (ValueError, TypeError, OverflowError):
        return failure("请求参数无效，请检查 URL、协议和数值设置。")
    except UpstreamError as error:
        return failure(error.public_message, 502)
    finally:
        if acquired:
            slots.release()


@app.post("/api/analyze")
def analyze():
    try:
        body = object_body()
        outputs = body.get("outputs")
        if not isinstance(outputs, list) or not 1 <= len(outputs) <= 3:
            raise ValueError("每轮归因必须提交 1–3 条回答。")
        items, seen = [], set()
        for output in outputs:
            if not isinstance(output, dict):
                raise ValueError("回答格式无效。")
            challenge = challenge_from(output.get("token"))
            if challenge["id"] in seen:
                raise ValueError("同一挑战不能重复计入。")
            seen.add(challenge["id"])
            content = output.get("text", "")
            if not isinstance(content, str) or len(content) > 100000:
                raise ValueError("回答内容无效或过长。")
            items.append({"text": content, "expected_count": challenge["expected_count"]})
        result = analyze_global_outputs(items, BANK)
        result.update(upstream_revision=REVISION, bank_sha256=BANK_SHA)
        return jsonify(result)
    except InputError as error:
        return failure(str(error))
    except (ValueError, TypeError):
        return failure("没有可用回答或输入格式无效；请粘贴完整数字序列，拒答或严重截断不会计入。")


if __name__ == "__main__":
    from waitress import serve
    bind = os.environ.get("BIND_HOST", "127.0.0.1")
    if bind not in {"127.0.0.1", "localhost", "::1"} and not os.environ.get("PANEL_ACCESS_TOKEN"):
        raise SystemExit("非本机监听必须配置 PANEL_ACCESS_TOKEN。")
    serve(app, host=bind, port=int(os.environ.get("PORT", "7860")), threads=6)
