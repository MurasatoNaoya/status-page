import hmac
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

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _require_admin_page():
    if not session.get("admin"):
        return redirect(url_for("admin.login"))
    return None


@admin_bp.route("/login", methods=["GET", "POST"])
def login():
    from status_page import app as app_module

    if request.method == "POST":
        if not app_module._check_csrf_token():
            flash("Invalid form submission. Please try again.")
            return render_template("admin_login.html")

        ip = request.remote_addr or "unknown"
        now = time.monotonic()

        with app_module._login_lock:
            attempts = [
                t
                for t in app_module._login_failures.get(ip, [])
                if now - t < app_module._LOGIN_WINDOW_SECONDS
            ]
            if attempts:
                app_module._login_failures[ip] = attempts
            else:
                app_module._login_failures.pop(ip, None)
            if (
                len(app_module._login_failures.get(ip, []))
                >= app_module._LOGIN_MAX_ATTEMPTS
            ):
                flash("Too many login attempts. Please try again later.")
                return render_template("admin_login.html"), 429

        user_ok = hmac.compare_digest(
            request.form.get("username", ""), app_module.ADMIN_USER
        )
        pass_ok = hmac.compare_digest(
            request.form.get("password", ""), app_module.ADMIN_PASS
        )
        if user_ok and pass_ok:
            with app_module._login_lock:
                app_module._login_failures.pop(ip, None)
            session["admin"] = True
            return redirect(url_for("admin.panel"))
        with app_module._login_lock:
            app_module._login_failures.setdefault(ip, []).append(now)
            if len(app_module._login_failures) > 10000:
                by_age = sorted(
                    app_module._login_failures.items(), key=lambda kv: max(kv[1])
                )
                for old_ip, _ in by_age[: len(app_module._login_failures) - 10000]:
                    del app_module._login_failures[old_ip]
        flash("Invalid credentials")
    return render_template("admin_login.html")


@admin_bp.route("/logout", methods=["POST"])
def logout():
    from status_page import app as app_module

    guard = _require_admin_page()
    if guard is not None:
        return guard
    if not app_module._check_csrf_token():
        flash("Invalid form submission. Please try again.")
        return redirect(url_for("admin.panel"))
    session.pop("admin", None)
    return redirect(url_for("public.index"))


@admin_bp.route("")
def panel():
    from status_page import app as app_module

    guard = _require_admin_page()
    if guard is not None:
        return guard
    active = app_module.get_active_incidents()
    recent = app_module.get_recent_incidents(limit=20)
    svc_names = [s["name"] for s in app_module.all_services()]
    integrations = {
        "SLACK_WEBHOOK_URL": app_module.os.environ.get("SLACK_WEBHOOK_URL"),
        "TEAMS_WEBHOOK_URL": app_module.os.environ.get("TEAMS_WEBHOOK_URL"),
        "JIRA_URL": app_module.os.environ.get("JIRA_URL"),
        "ALERT_EMAIL_TO": app_module.os.environ.get("ALERT_EMAIL_TO"),
        "ALERT_EMAIL_TO_MASKED": app_module._mask_email_list(
            app_module.os.environ.get("ALERT_EMAIL_TO")
        ),
        "SMTP_HOST": app_module.os.environ.get("SMTP_HOST"),
        "RESEND_API_KEY": bool(app_module.os.environ.get("RESEND_API_KEY")),
        "RESEND_FROM": app_module.os.environ.get("RESEND_FROM"),
    }
    return render_template(
        "admin.html",
        active_incidents=active,
        recent_incidents=recent,
        services=svc_names,
        config=integrations,
        feed_coverage=app_module.build_feed_coverage(app_module.STATUS_FEEDS),
    )


@admin_bp.route("/declare", methods=["POST"])
def declare_incident():
    from status_page import app as app_module

    guard = _require_admin_page()
    if guard is not None:
        return guard
    if not app_module._check_csrf_token():
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
    app_module.declare_incident_with_alerts(
        title=title, impact=impact, message=message, service_name=service
    )
    app_module._invalidate_index_cache("admin_declare_incident")
    flash(f"Incident declared: {title}")
    return redirect(url_for("admin.panel"))


@admin_bp.route("/update/<int:incident_id>", methods=["POST"])
def update_incident(incident_id):
    from status_page import app as app_module

    guard = _require_admin_page()
    if guard is not None:
        return guard
    if not app_module._check_csrf_token():
        flash("Invalid form submission. Please try again.")
        return redirect(url_for("admin.panel"))
    status = request.form["status"]
    message = request.form["message"][:2000]
    if status not in app_module._VALID_STATUSES:
        flash("Invalid status value.")
        return redirect(url_for("admin.panel"))
    if status == "resolved":
        resolved = app_module.resolve_incident_with_alerts(
            incident_id=incident_id, message=message
        )
        if not resolved:
            flash("Incident not found.")
            return redirect(url_for("admin.panel"))
    else:
        updated = app_module.update_incident(
            incident_id, status=status, message=message
        )
        if not updated:
            flash("Incident not found.")
            return redirect(url_for("admin.panel"))
    app_module._invalidate_index_cache("admin_update_incident")
    flash(f"Incident updated to: {status}")
    return redirect(url_for("admin.panel"))


@admin_bp.route("/backfill", methods=["POST"])
def backfill():
    from status_page import app as app_module

    guard = _require_admin_page()
    if guard is not None:
        return guard
    if not app_module._check_csrf_token():
        flash("Invalid form submission. Please try again.")
        return redirect(url_for("admin.panel"))
    with app_module.get_db() as db:
        before_count = db.execute(
            "SELECT COUNT(*) FROM incidents WHERE external_id IS NOT NULL"
        ).fetchone()[0]

    failed_feeds = []
    succeeded_feeds = 0
    for feed in app_module.STATUS_FEEDS:
        try:
            app_module.poll_status_feed(feed)
            succeeded_feeds += 1
            app_module.incr("backfill.feed.success")
        except Exception as e:
            app_module.logger.error(
                "Backfill failed for feed %s: %s", feed.get("name"), e
            )
            failed_feeds.append(feed.get("name", "unknown"))
            app_module.incr("backfill.feed.error")

    with app_module.get_db() as db:
        after_count = db.execute(
            "SELECT COUNT(*) FROM incidents WHERE external_id IS NOT NULL"
        ).fetchone()[0]

    imported = after_count - before_count
    app_module._invalidate_index_cache("admin_backfill")
    if failed_feeds:
        flash(
            "Backfill partially completed: "
            f"{imported} new incident(s), {succeeded_feeds}/{len(app_module.STATUS_FEEDS)} feed(s) succeeded. "
            f"Failed feed(s): {', '.join(failed_feeds)}."
        )
    else:
        flash(
            f"Backfill complete: {imported} new incident(s) imported from {len(app_module.STATUS_FEEDS)} feed(s)."
        )
    return redirect(url_for("admin.panel"))


@admin_bp.route("/test-email", methods=["POST"])
def test_email():
    from status_page import app as app_module

    guard = _require_admin_page()
    if guard is not None:
        return guard
    if not app_module._check_csrf_token():
        flash("Invalid form submission. Please try again.")
        return redirect(url_for("admin.panel"))
    sent = app_module.send_test_email()
    flash(
        "Test email sent."
        if sent
        else "Test email failed. Check email configuration and logs."
    )
    return redirect(url_for("admin.panel"))


@admin_bp.route("/feed-coverage")
def feed_coverage():
    from status_page import app as app_module

    guard = _require_admin_page()
    if guard is not None:
        return guard
    coverage = []
    for feed in app_module.STATUS_FEEDS:
        svc_names = set()
        svc_names.update(feed.get("components", {}).values())
        svc_names.update(feed.get("covered_services", []))
        stats = app_module.get_feed_incident_stats(svc_names)
        capability = app_module.get_feed_backfill_capability(feed)
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
    from status_page import app as app_module

    guard = _require_admin_page()
    if guard is not None:
        return guard
    if not app_module._check_csrf_token():
        flash("Invalid form submission. Please try again.")
        return redirect(url_for("admin.panel"))
    ok, msg = app_module.reload_runtime_config()
    flash(msg)
    return redirect(url_for("admin.panel"))
