import os
import shlex
import socket
import subprocess
import time
import xml.etree.ElementTree as ET

import requests


def check_http(service):
    url = service["url"]
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


def check_azure_status(service):
    """Check Azure status page for active incidents in a given region.

    Uses the main status page since the RSS feed has SSL cert issues.
    When the feed is empty (no incidents), the region is operational.
    Falls back to checking if the status page itself is reachable.
    """
    region = service.get("region", "UK South")
    timeout = service.get("timeout", 15)

    # Try RSS feed first (works from most environments)
    feed_url = "https://rssfeed.azure.status.microsoft/en-us/status/feed/"
    try:
        start = time.monotonic()
        resp = requests.get(feed_url, timeout=timeout)
        elapsed_ms = (time.monotonic() - start) * 1000

        if resp.status_code == 200:
            root = ET.fromstring(resp.content)
            items = root.findall(".//item")
            region_incidents = []
            for item in items:
                title = item.findtext("title", "")
                desc = item.findtext("description", "")
                if region.lower() in title.lower() or region.lower() in desc.lower():
                    region_incidents.append(title)

            if region_incidents:
                return "down", elapsed_ms, "; ".join(region_incidents[:3])
            return "up", elapsed_ms, None
    except (requests.RequestException, ET.ParseError):
        pass

    # Fallback: just check if the status page is reachable
    status_url = "https://azure.status.microsoft/en-us/status"
    try:
        start = time.monotonic()
        resp = requests.get(status_url, timeout=timeout)
        elapsed_ms = (time.monotonic() - start) * 1000
        if resp.status_code == 200:
            return "up", elapsed_ms, None
        return "down", elapsed_ms, f"Status page returned HTTP {resp.status_code}"
    except requests.RequestException as e:
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
    "azure_status": check_azure_status,
}


def run_check(service):
    check_type = service.get("type", "http")
    checker = CHECKERS.get(check_type)
    if not checker:
        return "down", None, f"Unknown check type: {check_type}"
    return checker(service)
