from flask import Blueprint

public_bp = Blueprint("public", __name__)


@public_bp.route("/")
def index():
    from status_page import app as app_module

    return app_module._render_index_cached()
