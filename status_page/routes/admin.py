import hmac
import os
import time

from flask import (
    Blueprint,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from status_page.runtime import get_runtime_context

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


@admin_bp.before_request
def _require_admin_for_non_login():
    if request.endpoint == "admin.login":
        return None
    if not session.get("admin"):
        return redirect(url_for("admin.login"))
    return None


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    ctx = get_runtime_context()

    if request.method == "POST":
        if not ctx["check_form_csrf"]():
            flash("Invalid form submission. Please try again.")
            return render_template("admin_login.html")

        ip = request.remote_addr or "unknown"
        now = time.monotonic()
        login_failures = ctx["login_failures"]
        login_lock = ctx["login_lock"]
        login_window = ctx["login_window_seconds"]
        login_max_attempts = ctx["login_max_attempts"]

        with login_lock:
            attempts = [t for t in login_failures.get(ip, []) if now - t < login_window]
            if attempts:
                login_failures[ip] = attempts
            else:
                login_failures.pop(ip, None)
            if len(login_failures.get(ip, [])) >= login_max_attempts:
                flash("Too many login attempts. Please try again later.")
                return render_template("admin_login.html"), 429

        user_ok = hmac.compare_digest(
            request.form.get("username", ""), ctx["admin_user"]
        )
        pass_ok = hmac.compare_digest(
            request.form.get("password", ""), ctx["admin_pass"]
        )
        if user_ok and pass_ok:
            with login_lock:
                login_failures.pop(ip, None)
            session["admin"] = True
            return redirect(url_for("admin.panel"))

        with login_lock:
            login_failures.setdefault(ip, []).append(now)
            if len(login_failures) > 10000:
                by_age = sorted(login_failures.items(), key=lambda kv: max(kv[1]))
                for old_ip, _ in by_age[: len(login_failures) - 10000]:
                    del login_failures[old_ip]
        flash("Invalid credentials")
    return render_template("admin_login.html")


@admin_bp.route("/logout", methods=["POST"])
def logout():
    ctx = get_runtime_context()
    if not ctx["check_form_csrf"]():
        flash("Invalid form submission. Please try again.")
        return redirect(url_for("admin.panel"))
    session.pop("admin", None)
    return redirect(url_for("public.index"))


@admin_bp.route("")
def panel():
    ctx = get_runtime_context()
    active = ctx["get_active_incidents"]()
    recent = ctx["get_recent_incidents"](limit=20)
    svc_names = [s["name"] for s in ctx["all_services"]()]
    integrations = {
        "SLACK_WEBHOOK_URL": os.environ.get("SLACK_WEBHOOK_URL"),
        "TEAMS_WEBHOOK_URL": os.environ.get("TEAMS_WEBHOOK_URL"),
        "JIRA_URL": os.environ.get("JIRA_URL"),
        "ALERT_EMAIL_TO": os.environ.get("ALERT_EMAIL_TO"),
        "ALERT_EMAIL_TO_MASKED": ctx["mask_email_list"](
            os.environ.get("ALERT_EMAIL_TO")
        ),
        "SMTP_HOST": os.environ.get("SMTP_HOST"),
        "RESEND_API_KEY": bool(os.environ.get("RESEND_API_KEY")),
        "RESEND_FROM": os.environ.get("RESEND_FROM"),
    }
    return render_template(
        "admin.html",
        active_incidents=active,
        recent_incidents=recent,
        services=svc_names,
        config=integrations,
        feed_coverage=ctx["build_feed_coverage"](ctx["status_feeds"]()),
    )


@admin_bp.route("/declare", methods=["POST"])
def declare_incident():
    ctx = get_runtime_context()
    if not ctx["check_form_csrf"]():
        flash("Invalid form submission. Please try again.")
        return redirect(url_for("admin.panel"))
    title = request.form.get("title", "").strip()[:255]
    if not title:
        flash("Incident title is required.")
        return redirect(url_for("admin.panel"))
    impact = request.form.get("impact", "partial")
    if impact not in ("major", "partial", "minor"):
        impact = "partial"
    message = request.form.get("message", "Investigating the issue.").strip()[:2000]
    service = request.form.get("service") or None
    ctx["declare_incident_with_alerts"](
        title=title, impact=impact, message=message, service_name=service
    )
    ctx["invalidate_index_cache"]("admin_declare_incident")
    flash(f"Incident declared: {title}")
    return redirect(url_for("admin.panel"))


@admin_bp.route("/update/<int:incident_id>", methods=["POST"])
def update_incident(incident_id):
    ctx = get_runtime_context()
    if not ctx["check_form_csrf"]():
        flash("Invalid form submission. Please try again.")
        return redirect(url_for("admin.panel"))
    status = request.form["status"]
    message = request.form["message"][:2000]
    if status not in ctx["valid_statuses"]:
        flash("Invalid status value.")
        return redirect(url_for("admin.panel"))
    if status == "resolved":
        resolved = ctx["resolve_incident_with_alerts"](
            incident_id=incident_id, message=message
        )
        if not resolved:
            flash("Incident not found.")
            return redirect(url_for("admin.panel"))
    else:
        updated = ctx["update_incident"](incident_id, status=status, message=message)
        if not updated:
            flash("Incident not found.")
            return redirect(url_for("admin.panel"))
    ctx["invalidate_index_cache"]("admin_update_incident")
    flash(f"Incident updated to: {status}")
    return redirect(url_for("admin.panel"))


@admin_bp.route("/backfill", methods=["POST"])
def backfill():
    ctx = get_runtime_context()
    if not ctx["check_form_csrf"]():
        flash("Invalid form submission. Please try again.")
        return redirect(url_for("admin.panel"))
    with ctx["get_db"]() as db:
        before_count = db.execute(
            "SELECT COUNT(*) FROM incidents WHERE external_id IS NOT NULL"
        ).fetchone()[0]

    failed_feeds = []
    succeeded_feeds = 0
    for feed in ctx["status_feeds"]():
        try:
            ctx["poll_status_feed"](feed)
            succeeded_feeds += 1
            ctx["incr"]("backfill.feed.success")
        except Exception as e:
            ctx["logger"].error("Backfill failed for feed %s: %s", feed.get("name"), e)
            failed_feeds.append(feed.get("name", "unknown"))
            ctx["incr"]("backfill.feed.error")

    with ctx["get_db"]() as db:
        after_count = db.execute(
            "SELECT COUNT(*) FROM incidents WHERE external_id IS NOT NULL"
        ).fetchone()[0]

    imported = after_count - before_count
    ctx["invalidate_index_cache"]("admin_backfill")
    if failed_feeds:
        flash(
            "Backfill partially completed: "
            f"{imported} new incident(s), {succeeded_feeds}/{len(ctx['status_feeds']())} feed(s) succeeded. "
            f"Failed feed(s): {', '.join(failed_feeds)}."
        )
    else:
        flash(
            f"Backfill complete: {imported} new incident(s) imported from {len(ctx['status_feeds']())} feed(s)."
        )
    return redirect(url_for("admin.panel"))


@admin_bp.route("/test-email", methods=["POST"])
def test_email():
    ctx = get_runtime_context()
    if not ctx["check_form_csrf"]():
        flash("Invalid form submission. Please try again.")
        return redirect(url_for("admin.panel"))
    sent = ctx["send_test_email"]()
    flash(
        "Test email sent."
        if sent
        else "Test email failed. Check email configuration and logs."
    )
    return redirect(url_for("admin.panel"))


@admin_bp.route("/feed-coverage")
def feed_coverage():
    ctx = get_runtime_context()
    coverage = []
    for feed in ctx["status_feeds"]():
        svc_names = set()
        svc_names.update(feed.get("components", {}).values())
        svc_names.update(feed.get("covered_services", []))
        stats = ctx["get_feed_incident_stats"](svc_names)
        capability = ctx["get_feed_backfill_capability"](feed)
        coverage.append(
            {
                "feed": feed.get("name"),
                "feed_type": capability["feed_type"],
                "ingestion": capability["ingestion"],
                "known_limit_days": capability["known_limit_days"],
                "cap_type": capability["cap_type"],
                "cap_summary": capability["cap_summary"],
                "services": sorted(svc_names),
                "incident_count": stats["cnt"],
                "oldest": stats["oldest"],
                "newest": stats["newest"],
            }
        )
    return jsonify(coverage)


@admin_bp.route("/reload-config", methods=["POST"])
def reload_config():
    ctx = get_runtime_context()
    if not ctx["check_form_csrf"]():
        flash("Invalid form submission. Please try again.")
        return redirect(url_for("admin.panel"))
    ok, msg = ctx["reload_runtime_config"]()
    flash(msg)
    return redirect(url_for("admin.panel"))
