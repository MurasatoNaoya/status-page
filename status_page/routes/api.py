from flask import Blueprint, jsonify, request, session

from status_page.runtime import get_runtime_context

api_bp = Blueprint("api", __name__, url_prefix="/api")


def _require_admin_api():
    if not session.get("admin"):
        return jsonify({"error": "Authentication required"}), 401
    return None


@api_bp.route("/incidents", methods=["POST"])
def create_incident():
    ctx = get_runtime_context()

    guard = _require_admin_api()
    if guard is not None:
        return guard
    if not ctx["check_api_csrf"]():
        return jsonify({"error": "Missing or invalid CSRF token"}), 403
    data = request.json
    if not data or "title" not in data:
        return jsonify({"error": "Missing required field: title"}), 400
    impact = data.get("impact", "minor")
    if impact not in {"major", "partial", "minor"}:
        return jsonify(
            {"error": "Invalid impact. Must be one of: major, minor, partial"}
        ), 400
    incident_id = ctx["create_incident"](
        title=data["title"][:255],
        impact=impact,
        message=data.get("message", "Investigating the issue.")[:2000],
        service_name=data.get("service_name"),
    )
    ctx["invalidate_index_cache"]("api_create_incident")
    return jsonify({"id": incident_id}), 201


@api_bp.route("/incidents/<int:incident_id>", methods=["PATCH"])
def update_incident(incident_id):
    ctx = get_runtime_context()

    guard = _require_admin_api()
    if guard is not None:
        return guard
    if not ctx["check_api_csrf"]():
        return jsonify({"error": "Missing or invalid CSRF token"}), 403
    data = request.json
    if not data or "status" not in data or "message" not in data:
        return jsonify({"error": "Missing required fields: status, message"}), 400
    if data["status"] not in ctx["valid_statuses"]:
        return jsonify(
            {
                "error": f"Invalid status. Must be one of: {', '.join(sorted(ctx['valid_statuses']))}"
            }
        ), 400
    updated = ctx["update_incident"](
        incident_id, status=data["status"], message=data["message"][:2000]
    )
    if not updated:
        return jsonify({"error": "Incident not found"}), 404
    ctx["invalidate_index_cache"]("api_update_incident")
    return jsonify({"ok": True})


@api_bp.route("/health")
def health():
    ctx = get_runtime_context()
    service_names = [s["name"] for s in ctx["all_services"]()]
    latest = ctx["get_latest_status"](service_names)
    return jsonify(
        {
            name: {
                "status": info["status"],
                "response_time_ms": info["response_time_ms"],
            }
            if info
            else {"status": "unknown"}
            for name, info in latest.items()
        }
    )


@api_bp.route("/health/scheduler")
def scheduler_health():
    ctx = get_runtime_context()
    health_data = ctx["get_scheduler_health"]()
    code = 200 if health_data["status"] in {"healthy", "disabled"} else 503
    return jsonify(health_data), code


@api_bp.route("/metrics")
def metrics():
    ctx = get_runtime_context()

    guard = _require_admin_api()
    if guard is not None:
        return guard
    return jsonify(ctx["snapshot"]())
