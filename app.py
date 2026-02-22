"""Compatibility entrypoint for Gunicorn and local runs.

Primary application code lives in status_page.app.
"""

from status_page.app import app, main

__all__ = ["app", "main"]


if __name__ == "__main__":
    main()
