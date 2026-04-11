"""
Flask frontend proxy routes for WeBot runtime APIs.
"""

from flask import jsonify, request, session
import requests


def register_webot_routes(
    app,
    *,
    port_agent: int,
    internal_token: str,
) -> None:
    base_url = f"http://127.0.0.1:{port_agent}"

    def _internal_auth_headers():
        return {"X-Internal-Token": internal_token}

    @app.route("/proxy_webot_subagents")
    def proxy_webot_subagents():
        user_id = session.get("user_id", "")
        try:
            response = requests.get(
                f"{base_url}/webot/subagents",
                params={"user_id": user_id},
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_subagent_history", methods=["POST"])
    def proxy_webot_subagent_history():
        user_id = session.get("user_id", "")
        body = request.get_json(force=True) if request.is_json else {}
        try:
            response = requests.post(
                f"{base_url}/webot/subagents/history",
                json={
                    "user_id": user_id,
                    "agent_ref": body.get("agent_ref", ""),
                    "limit": body.get("limit", 12),
                },
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_subagent_cancel", methods=["POST"])
    def proxy_webot_subagent_cancel():
        user_id = session.get("user_id", "")
        body = request.get_json(force=True) if request.is_json else {}
        try:
            response = requests.post(
                f"{base_url}/webot/subagents/cancel",
                json={
                    "user_id": user_id,
                    "agent_ref": body.get("agent_ref", ""),
                },
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_tool_policy")
    def proxy_webot_tool_policy():
        user_id = session.get("user_id", "")
        try:
            response = requests.get(
                f"{base_url}/webot/tool-policy",
                params={"user_id": user_id},
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_tool_policy", methods=["POST"])
    def proxy_webot_tool_policy_update():
        user_id = session.get("user_id", "")
        body = request.get_json(force=True) if request.is_json else {}
        try:
            response = requests.post(
                f"{base_url}/webot/tool-policy",
                json={
                    "user_id": user_id,
                    "policy": body.get("policy") or {},
                },
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_tool_approvals")
    def proxy_webot_tool_approvals():
        user_id = session.get("user_id", "")
        status = request.args.get("status", "pending")
        session_id = request.args.get("session_id", "")
        limit = request.args.get("limit", "20")
        try:
            response = requests.get(
                f"{base_url}/webot/tool-approvals",
                params={
                    "user_id": user_id,
                    "status": status,
                    "session_id": session_id,
                    "limit": limit,
                },
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc), "approvals": []}), 500

    @app.route("/proxy_webot_session_runtime")
    def proxy_webot_session_runtime():
        user_id = session.get("user_id", "")
        session_id = request.args.get("session_id", "")
        try:
            response = requests.get(
                f"{base_url}/webot/session-runtime",
                params={"user_id": user_id, "session_id": session_id},
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_session_mode", methods=["POST"])
    def proxy_webot_session_mode():
        user_id = session.get("user_id", "")
        body = request.get_json(force=True) if request.is_json else {}
        try:
            response = requests.post(
                f"{base_url}/webot/session-mode",
                json={
                    "user_id": user_id,
                    "session_id": body.get("session_id", ""),
                    "mode": body.get("mode", "execute"),
                    "reason": body.get("reason", ""),
                },
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_workflow_presets")
    def proxy_webot_workflow_presets():
        user_id = session.get("user_id", "")
        try:
            response = requests.get(
                f"{base_url}/webot/workflow-presets",
                params={"user_id": user_id},
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_workflow_apply", methods=["POST"])
    def proxy_webot_workflow_apply():
        user_id = session.get("user_id", "")
        body = request.get_json(force=True) if request.is_json else {}
        try:
            response = requests.post(
                f"{base_url}/webot/workflow-presets/apply",
                json={
                    "user_id": user_id,
                    "session_id": body.get("session_id", ""),
                    "preset_id": body.get("preset_id", ""),
                },
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_session_inbox")
    def proxy_webot_session_inbox():
        user_id = session.get("user_id", "")
        try:
            response = requests.get(
                f"{base_url}/webot/session-inbox",
                params={
                    "user_id": user_id,
                    "session_id": request.args.get("session_id", ""),
                    "target_ref": request.args.get("target_ref", ""),
                    "status": request.args.get("status", "queued"),
                    "limit": request.args.get("limit", 20),
                },
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_session_inbox_send", methods=["POST"])
    def proxy_webot_session_inbox_send():
        user_id = session.get("user_id", "")
        body = request.get_json(force=True) if request.is_json else {}
        try:
            response = requests.post(
                f"{base_url}/webot/session-inbox/send",
                json={
                    "user_id": user_id,
                    "session_id": body.get("session_id", ""),
                    "target_ref": body.get("target_ref", ""),
                    "body": body.get("body", ""),
                },
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_session_inbox_deliver", methods=["POST"])
    def proxy_webot_session_inbox_deliver():
        user_id = session.get("user_id", "")
        body = request.get_json(force=True) if request.is_json else {}
        try:
            response = requests.post(
                f"{base_url}/webot/session-inbox/deliver",
                json={
                    "user_id": user_id,
                    "session_id": body.get("session_id", ""),
                    "target_ref": body.get("target_ref", ""),
                    "limit": body.get("limit", 20),
                    "force": bool(body.get("force", False)),
                },
                headers=_internal_auth_headers(),
                timeout=20,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_run_interrupt", methods=["POST"])
    def proxy_webot_run_interrupt():
        user_id = session.get("user_id", "")
        body = request.get_json(force=True) if request.is_json else {}
        try:
            response = requests.post(
                f"{base_url}/webot/runs/interrupt",
                json={
                    "user_id": user_id,
                    "session_id": body.get("session_id", ""),
                    "run_id": body.get("run_id", ""),
                    "agent_ref": body.get("agent_ref", ""),
                },
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_voice", methods=["POST"])
    def proxy_webot_voice():
        user_id = session.get("user_id", "")
        body = request.get_json(force=True) if request.is_json else {}
        try:
            response = requests.post(
                f"{base_url}/webot/voice",
                json={
                    "user_id": user_id,
                    "session_id": body.get("session_id", ""),
                    "enabled": bool(body.get("enabled", False)),
                    "auto_read_aloud": bool(body.get("auto_read_aloud", False)),
                    "last_transcript": body.get("last_transcript", ""),
                    "tts_model": body.get("tts_model", ""),
                    "tts_voice": body.get("tts_voice", ""),
                    "stt_model": body.get("stt_model", ""),
                },
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_bridge_attach", methods=["POST"])
    def proxy_webot_bridge_attach():
        user_id = session.get("user_id", "")
        body = request.get_json(force=True) if request.is_json else {}
        try:
            response = requests.post(
                f"{base_url}/webot/bridge/attach",
                json={
                    "user_id": user_id,
                    "session_id": body.get("session_id", ""),
                    "role": body.get("role", "viewer"),
                    "label": body.get("label", ""),
                },
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_bridge_detach", methods=["POST"])
    def proxy_webot_bridge_detach():
        user_id = session.get("user_id", "")
        body = request.get_json(force=True) if request.is_json else {}
        try:
            response = requests.post(
                f"{base_url}/webot/bridge/detach",
                json={
                    "user_id": user_id,
                    "bridge_id": body.get("bridge_id", ""),
                },
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_kairos", methods=["POST"])
    def proxy_webot_kairos():
        user_id = session.get("user_id", "")
        body = request.get_json(force=True) if request.is_json else {}
        try:
            response = requests.post(
                f"{base_url}/webot/kairos",
                json={
                    "user_id": user_id,
                    "session_id": body.get("session_id", ""),
                    "enabled": bool(body.get("enabled", False)),
                    "reason": body.get("reason", ""),
                },
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_dream", methods=["POST"])
    def proxy_webot_dream():
        user_id = session.get("user_id", "")
        body = request.get_json(force=True) if request.is_json else {}
        try:
            response = requests.post(
                f"{base_url}/webot/dream",
                json={
                    "user_id": user_id,
                    "session_id": body.get("session_id", ""),
                    "reason": body.get("reason", ""),
                },
                headers=_internal_auth_headers(),
                timeout=30,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_buddy", methods=["POST"])
    def proxy_webot_buddy():
        user_id = session.get("user_id", "")
        body = request.get_json(force=True) if request.is_json else {}
        try:
            response = requests.post(
                f"{base_url}/webot/buddy",
                json={
                    "user_id": user_id,
                    "session_id": body.get("session_id", ""),
                    "action": body.get("action", "pet"),
                },
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500

    @app.route("/proxy_webot_tool_approval_resolve", methods=["POST"])
    def proxy_webot_tool_approval_resolve():
        user_id = session.get("user_id", "")
        body = request.get_json(force=True) if request.is_json else {}
        try:
            response = requests.post(
                f"{base_url}/webot/tool-approvals/resolve",
                json={
                    "user_id": user_id,
                    "approval_id": body.get("approval_id", ""),
                    "action": body.get("action", "approve"),
                    "reason": body.get("reason", ""),
                    "remember": bool(body.get("remember", False)),
                    "session_id": body.get("session_id", ""),
                },
                headers=_internal_auth_headers(),
                timeout=15,
            )
            return jsonify(response.json()), response.status_code
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
