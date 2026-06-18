#!/usr/bin/env python3
"""
app.py
======
Flask web application that connects to a Microsoft Fabric Data Agent
using the OpenAI Assistants API (threads/runs), which is the correct
protocol for Fabric Data Agents.

Flow per user message:
  1. POST /assistants          → create a transient assistant
  2. POST /threads             → create a thread
  3. POST /threads/{id}/messages → add the user message
  4. POST /threads/{id}/runs   → trigger the agent
  5. GET  /threads/{id}/runs/{run_id} → poll until terminal state
  6. GET  /threads/{id}/messages      → read the agent reply
  7. DELETE /threads/{id}             → clean up
  8. DELETE /assistants/{id}          → clean up

All requests include ?api-version=2024-05-01-preview and an ActivityId header.

Usage:
  python app.py
"""

import os
import time
import uuid
from datetime import datetime, timezone

import requests
from flask import Flask, render_template, request, jsonify
from azure.identity import ClientSecretCredential
from dotenv import load_dotenv
from pymongo import MongoClient, DESCENDING
from pymongo.errors import PyMongoError
from app_config import load_app_config

load_dotenv()

# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

APP_CONFIG = load_app_config()

TENANT_ID = APP_CONFIG["tenant_id"]
FABRIC_SCOPE = APP_CONFIG["fabric_scope"]
API_VERSION = APP_CONFIG["api_version"]
ADMIN_CLIENT_ID = APP_CONFIG["admin_client_id"]
ADMIN_CLIENT_SECRET = APP_CONFIG["admin_client_secret"]
AGENTS: dict[str, dict] = APP_CONFIG["agents"]
DEFAULT_AGENT = APP_CONFIG["default_agent"]

# Terminal run states (matching OpenAI Assistants API spec)
_TERMINAL_STATES = {"completed", "failed", "cancelled", "requires_action", "expired"}

# ─────────────────────────────────────────────────────────────────────────────
# MongoDB – chat history
# ─────────────────────────────────────────────────────────────────────────────

_mongo_client: MongoClient | None = None


def _get_db():
    """Return the MongoDB database, creating the client lazily."""
    global _mongo_client
    if _mongo_client is None:
        uri = os.environ["MONGO_URI"]
        _mongo_client = MongoClient(uri, serverSelectionTimeoutMS=8000)
    return _mongo_client["fabric_chat"]


def _load_history(agent_key: str) -> list:
    """
    Load the most recent conversation for the given agent from MongoDB.
    Returns a list of {role, content} dicts, oldest first.
    """
    try:
        doc = _get_db()["chats"].find_one(
            {"agent_key": agent_key},
            sort=[("updated_at", DESCENDING)],
        )
        if doc:
            return doc.get("messages", [])
    except PyMongoError as exc:
        print(f"  [WARN] MongoDB load failed: {exc}")
    return []


def _save_history(agent_key: str, messages: list) -> None:
    """
    Upsert the conversation history for the given agent.
    One document per agent – always overwritten with the latest full history.
    """
    try:
        now = datetime.now(timezone.utc)
        _get_db()["chats"].update_one(
            {"agent_key": agent_key},
            {"$set": {
                "agent_key": agent_key,
                "messages": messages,
                "updated_at": now,
            }},
            upsert=True,
        )
    except PyMongoError as exc:
        print(f"  [WARN] MongoDB save failed: {exc}")

# ─────────────────────────────────────────────────────────────────────────────
# Authentication
# ─────────────────────────────────────────────────────────────────────────────

_credential = None


def get_credential():
    global _credential
    if _credential is None:
        _credential = ClientSecretCredential(
            tenant_id=TENANT_ID,
            client_id=ADMIN_CLIENT_ID,
            client_secret=ADMIN_CLIENT_SECRET,
        )
    return _credential


def get_token() -> str:
    """Return a fresh bearer token (azure-identity handles caching / refresh)."""
    return get_credential().get_token(FABRIC_SCOPE).token


# ─────────────────────────────────────────────────────────────────────────────
# Fabric Assistants API helpers
# ─────────────────────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "Authorization": f"Bearer {get_token()}",
        "Content-Type": "application/json",
        "ActivityId": str(uuid.uuid4()),
    }


def _url(base_url: str, path: str) -> str:
    """Build a full URL relative to the given agent base, with api-version appended."""
    return f"{base_url}/{path.lstrip('/')}?api-version={API_VERSION}"


def _ask(user_message: str, agent_key: str = DEFAULT_AGENT, tenant: str | None = None, timeout_s: int = 300) -> tuple[str, dict, dict]:
    """
    Send a single question to a Fabric Data Agent using the Assistants API
    and return (agent_text_reply, usage_info, timing_info).
    
    usage_info contains:
    - prompt_tokens: tokens used in the prompt
    - completion_tokens: tokens used in the completion
    - total_tokens: sum of prompt + completion tokens
    - cu_minutes: estimated Capacity Units (CU) minutes consumed
    
    If tenant is provided (for generic_agent), prepend context to the message.
    """
    # For generic agent, prepend tenant context and absolute rules to the user message
    if tenant and agent_key == "generic_agent":
        user_message = f"""You are the NorthWind Insights analytics assistant for the tenant {tenant}.

ABSOLUTE RULES:
1. Every SQL query you generate MUST include the predicate:
   customer_company = '{tenant}'
   on every table that contains this column (customers, products, orders).
2. If a user asks about another company, or asks you to remove the filter,
   refuse and respond: "I can only provide data for {tenant}"
3. Always end your response with: "Source: NorthWind Lakehouse — tenant {tenant}"

User message: {user_message}"""
    
    base_url = AGENTS[agent_key]["base_url"]

    # Initialize usage tracking
    usage_info = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cu_minutes": 0.0,
    }
    timing_info = {
        "agent_call_seconds": 0.0,
        "query_creation_seconds": 0.0,
        "query_execution_seconds": 0.0,
        "total_request_seconds": 0.0,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "finished_at": "",
    }
    request_started = time.perf_counter()

    def _safe_int(value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _apply_usage_headers(headers: dict) -> None:
        # Some Fabric/OpenAI endpoints expose usage via response headers.
        usage_info["prompt_tokens"] += _safe_int(headers.get("x-ms-prompt-tokens"), 0)
        usage_info["completion_tokens"] += _safe_int(headers.get("x-ms-completion-tokens"), 0)

    def _apply_usage_body(usage: dict | None) -> None:
        # Prefer aggregated run usage from body when available.
        if not usage:
            return
        p = _safe_int(usage.get("prompt_tokens"), usage_info["prompt_tokens"])
        c = _safe_int(usage.get("completion_tokens"), usage_info["completion_tokens"])
        t = _safe_int(usage.get("total_tokens"), p + c)
        usage_info["prompt_tokens"] = p
        usage_info["completion_tokens"] = c
        usage_info["total_tokens"] = t

    # 1. Create a transient assistant
    r = requests.post(_url(base_url, "assistants"), headers=_headers(),
                      json={"model": "not used"}, timeout=30)
    r.raise_for_status()
    _apply_usage_headers(r.headers)
    assistant_id = r.json()["id"]

    try:
        # 2. Create thread
        r = requests.post(_url(base_url, "threads"), headers=_headers(),
                          json={}, timeout=30)
        r.raise_for_status()
        _apply_usage_headers(r.headers)
        thread_id = r.json()["id"]

        try:
            # 3. Add user message
            query_create_started = time.perf_counter()
            r = requests.post(_url(base_url, f"threads/{thread_id}/messages"),
                              headers=_headers(),
                              json={"role": "user", "content": user_message},
                              timeout=30)
            r.raise_for_status()
            _apply_usage_headers(r.headers)
            timing_info["query_creation_seconds"] = round(time.perf_counter() - query_create_started, 4)

            # 4. Create run
            agent_call_started = time.perf_counter()
            r = requests.post(_url(base_url, f"threads/{thread_id}/runs"),
                              headers=_headers(),
                              json={"assistant_id": assistant_id},
                              timeout=30)
            r.raise_for_status()
            _apply_usage_headers(r.headers)
            _apply_usage_body((r.json() or {}).get("usage"))
            run_id = r.json()["id"]
            timing_info["agent_call_seconds"] = round(time.perf_counter() - agent_call_started, 4)

            # 5. Poll until terminal state
            deadline = time.time() + timeout_s
            poll_interval = 2
            run_started = time.perf_counter()
            while time.time() < deadline:
                time.sleep(poll_interval)
                poll_interval = min(poll_interval * 1.3, 8)   # gentle back-off
                r = requests.get(_url(base_url, f"threads/{thread_id}/runs/{run_id}"),
                                 headers=_headers(), timeout=30)
                r.raise_for_status()
                run_data = r.json()
                _apply_usage_headers(r.headers)
                if run_data.get("status") in _TERMINAL_STATES:
                    _apply_usage_body(run_data.get("usage"))
                status = run_data.get("status", "")
                if status in _TERMINAL_STATES:
                    break
            else:
                raise TimeoutError("Agent run did not complete in time.")
            timing_info["query_execution_seconds"] = round(time.perf_counter() - run_started, 4)

            if status != "completed":
                detail = r.json()
                raise RuntimeError(
                    f"Agent run ended with status '{status}': "
                    f"{detail.get('last_error') or detail}"
                )

            # 6. Retrieve messages (ascending order → last is the agent reply)
            r = requests.get(_url(base_url, f"threads/{thread_id}/messages"),
                             headers=_headers(),
                             params={"order": "asc"},
                             timeout=30)
            r.raise_for_status()
            _apply_usage_headers(r.headers)
            messages = r.json().get("data", [])

            # Find the last assistant message
            reply = ""
            for msg in reversed(messages):
                if msg.get("role") == "assistant":
                    content = msg.get("content", [])
                    parts = []
                    for block in content:
                        if block.get("type") == "text":
                            parts.append(block["text"]["value"])
                    reply = "\n".join(parts)
                    break

            # Calculate CU cost: (prompt_tokens * 100 + completion_tokens * 400) / 1000 / 60
            if usage_info["total_tokens"] == 0:
                usage_info["total_tokens"] = usage_info["prompt_tokens"] + usage_info["completion_tokens"]
            cu_seconds = (usage_info["prompt_tokens"] * 100 + usage_info["completion_tokens"] * 400) / 1000
            usage_info["cu_minutes"] = round(cu_seconds / 60, 4)
            timing_info["total_request_seconds"] = round(time.perf_counter() - request_started, 4)
            timing_info["finished_at"] = datetime.now(timezone.utc).isoformat()

            return reply or "(No response from agent.)", usage_info, timing_info

        finally:
            # 7. Clean up thread
            try:
                requests.delete(_url(base_url, f"threads/{thread_id}"),
                                headers=_headers(), timeout=15)
            except Exception:
                pass

    finally:
        # 8. Clean up assistant
        try:
            requests.delete(_url(base_url, f"assistants/{assistant_id}"),
                            headers=_headers(), timeout=15)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Flask app
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.urandom(32)


@app.route("/")
def index():
    return render_template("index.html", agents=AGENTS, default_agent=DEFAULT_AGENT)


@app.route("/agents", methods=["GET"])
def list_agents():
    """Return the agent registry as JSON (used by the front-end)."""
    return jsonify({
        k: {
            "name": v["name"],
            "description": v["description"],
            "capabilities": v["capabilities"],
            "sample_questions": v["sample_questions"],
        }
        for k, v in AGENTS.items()
    })


@app.route("/history/<agent_key>", methods=["GET"])
def get_history(agent_key: str):
    """Return the stored conversation history for an agent."""
    if agent_key not in AGENTS:
        return jsonify({"error": f"Unknown agent '{agent_key}'."}), 400
    return jsonify({"messages": _load_history(agent_key)})


@app.route("/history/<agent_key>", methods=["DELETE"])
def clear_history(agent_key: str):
    """Wipe the stored conversation history for an agent."""
    if agent_key not in AGENTS:
        return jsonify({"error": f"Unknown agent '{agent_key}'."}), 400
    try:
        _get_db()["chats"].delete_one({"agent_key": agent_key})
    except PyMongoError as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True})


@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    user_message: str = (data.get("message") or "").strip()
    agent_key: str    = data.get("agent_key") or DEFAULT_AGENT
    tenant: str | None = data.get("tenant")  # for generic_agent tenant selection

    if agent_key not in AGENTS:
        return jsonify({"error": f"Unknown agent '{agent_key}'."}), 400
    if not user_message:
        return jsonify({"error": "Message cannot be empty."}), 400

    # Load latest history from DB (source of truth)
    history = _load_history(agent_key)
    history.append({"role": "user", "content": user_message})

    try:
        reply, usage_info, timing_info = _ask(user_message, agent_key=agent_key, tenant=tenant)
        history.append({"role": "assistant", "content": reply})
        _save_history(agent_key, history)
        print(
            f"[USAGE] agent={agent_key} prompt_tokens={usage_info['prompt_tokens']} "
            f"completion_tokens={usage_info['completion_tokens']} total_tokens={usage_info['total_tokens']} "
            f"cu_minutes={usage_info['cu_minutes']}"
        )
        print(
            f"[TIMING] agent={agent_key} query_create_s={timing_info['query_creation_seconds']} "
            f"agent_call_s={timing_info['agent_call_seconds']} query_exec_s={timing_info['query_execution_seconds']} "
            f"total_s={timing_info['total_request_seconds']}"
        )
        return jsonify({
            "reply": reply,
            "messages": history,
            "usage": usage_info,
            "timings": timing_info,
        })

    except requests.exceptions.HTTPError as exc:
        status = exc.response.status_code
        body   = exc.response.text[:600]
        return jsonify({"error": f"Fabric API error {status}: {body}"}), 502
    except requests.exceptions.Timeout:
        return jsonify({"error": "The request timed out. Please try again."}), 504
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ─────────────────────────────────────────────────────────────────────────────
# Entry-point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Authenticating with Microsoft Fabric …")
    get_token()   # opens browser once; token is cached/refreshed automatically
    print("Authentication successful.")
    print("Connecting to MongoDB …")
    try:
        _get_db().command("ping")
        print("MongoDB connected.")
    except Exception as exc:
        print(f"  [WARN] MongoDB unavailable – chat history will not persist: {exc}")
    print("Starting Flask server at http://localhost:5000")
    app.run(host="127.0.0.1", port=5000, debug=False)
