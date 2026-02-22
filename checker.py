import os
import shlex
import socket
import subprocess
import time
import requests


def check_http(service):
    url = service["url"]
    if not url.startswith(("http://", "https://")):
        return "down", None, f"Invalid URL scheme: {url}"
    timeout = service.get("timeout", 10)
    expected_status = service.get("expected_status", 200)
    headers = {}
    token_env = service.get("auth_token_env")
    if token_env:
        token = os.environ.get(token_env)
        if token:
            headers["Authorization"] = f"token {token}"
    try:
        start = time.monotonic()
        resp = requests.get(url, timeout=timeout, allow_redirects=True, headers=headers)
        elapsed_ms = (time.monotonic() - start) * 1000
        if resp.status_code == expected_status:
            return "up", elapsed_ms, None
        # Treat rate limits as "up" — the service is working, we're just throttled
        if resp.status_code in (403, 429) and "rate limit" in resp.text.lower():
            return "up", elapsed_ms, None
        return "down", elapsed_ms, f"HTTP {resp.status_code}"
    except requests.RequestException as e:
        return "down", None, str(e)


def check_tcp(service):
    host = service["host"]
    port = service["port"]
    timeout = service.get("timeout", 5)
    try:
        start = time.monotonic()
        sock = socket.create_connection((host, port), timeout=timeout)
        elapsed_ms = (time.monotonic() - start) * 1000
        sock.close()
        return "up", elapsed_ms, None
    except (socket.timeout, OSError) as e:
        return "down", None, str(e)


def check_dns(service):
    hostname = service["hostname"]
    try:
        start = time.monotonic()
        socket.getaddrinfo(hostname, None)
        elapsed_ms = (time.monotonic() - start) * 1000
        return "up", elapsed_ms, None
    except socket.gaierror as e:
        return "down", None, str(e)


# SECURITY: Commands are read from config.yaml which must be a trusted,
# read-only file. shell=True is NOT used — shlex.split prevents injection.
# If config.yaml is writable by untrusted parties, this is an RCE vector.
def check_script(service):
    command = service["command"]
    timeout = service.get("timeout", 30)
    try:
        start = time.monotonic()
        args = shlex.split(command) if isinstance(command, str) else command
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        elapsed_ms = (time.monotonic() - start) * 1000
        if result.returncode == 0:
            return "up", elapsed_ms, None
        return (
            "down",
            elapsed_ms,
            result.stderr.strip() or f"Exit code {result.returncode}",
        )
    except subprocess.TimeoutExpired:
        return "down", None, "Script timed out"
    except Exception as e:
        return "down", None, str(e)


def check_dns_bar(targets):
    """Resolve multiple DNS targets and return per-target results.

    Returns list of {label, hostname, status, ms, error}.
    """
    results = []
    for target in targets:
        hostname = target["hostname"]
        label = target.get("label", hostname)
        try:
            start = time.monotonic()
            socket.getaddrinfo(hostname, None)
            elapsed_ms = (time.monotonic() - start) * 1000
            results.append(
                {
                    "label": label,
                    "hostname": hostname,
                    "status": "up",
                    "ms": round(elapsed_ms, 1),
                    "error": None,
                }
            )
        except socket.gaierror as e:
            results.append(
                {
                    "label": label,
                    "hostname": hostname,
                    "status": "down",
                    "ms": None,
                    "error": str(e),
                }
            )
    return results


CHECKERS = {
    "http": check_http,
    "tcp": check_tcp,
    "dns": check_dns,
    "script": check_script,
}


def run_check(service):
    check_type = service.get("type", "http")
    checker = CHECKERS.get(check_type)
    if not checker:
        return "down", None, f"Unknown check type: {check_type}"
    return checker(service)
