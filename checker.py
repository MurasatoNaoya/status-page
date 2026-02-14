import socket
import subprocess
import time

import requests


def check_http(service):
    url = service["url"]
    timeout = service.get("timeout", 10)
    expected_status = service.get("expected_status", 200)
    try:
        start = time.monotonic()
        resp = requests.get(url, timeout=timeout, allow_redirects=True)
        elapsed_ms = (time.monotonic() - start) * 1000
        if resp.status_code == expected_status:
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


def check_script(service):
    command = service["command"]
    timeout = service.get("timeout", 30)
    try:
        start = time.monotonic()
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout
        )
        elapsed_ms = (time.monotonic() - start) * 1000
        if result.returncode == 0:
            return "up", elapsed_ms, None
        return "down", elapsed_ms, result.stderr.strip() or f"Exit code {result.returncode}"
    except subprocess.TimeoutExpired:
        return "down", None, "Script timed out"
    except Exception as e:
        return "down", None, str(e)


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
