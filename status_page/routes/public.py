from flask import Blueprint

from status_page.runtime import get_runtime_context

public_bp = Blueprint("public", __name__)


@public_bp.route("/")
def index():
    ctx = get_runtime_context()
    return ctx["render_index_cached"]()
