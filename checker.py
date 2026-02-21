import os
import shlex
import socket
import subprocess
import time
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


# --- Azure credential helper (shared by both checkers) ---
# Creates a single CertificateCredential instance so token caching works
# across repeated check invocations (CertificateCredential caches internally).
#
# _azure_credential = None
#
#
# def _get_azure_credential():
#     global _azure_credential
#     if _azure_credential is None:
#         from azure.identity import CertificateCredential
#
#         _azure_credential = CertificateCredential(
#             tenant_id=os.environ["AZURE_TENANT_ID"],
#             client_id=os.environ["AZURE_CLIENT_ID"],
#             certificate_path=os.environ["AZURE_CLIENT_CERTIFICATE_PATH"],
#         )
#     return _azure_credential


# --- Azure Service Health (subscription-wide incidents) ---
# Requires: azure-identity, requests
# Env vars: AZURE_TENANT_ID, AZURE_CLIENT_ID,
#           AZURE_CLIENT_CERTIFICATE_PATH (.pfx), AZURE_SUBSCRIPTION_ID
#
# def check_azure_service_health(service):
#     credential = _get_azure_credential()
#     token = credential.get_token("https://management.azure.com/.default").token
#     sub_id = service.get("subscription_id", os.environ["AZURE_SUBSCRIPTION_ID"])
#
#     # NOTE: $filter value may need "properties/eventType" — verify against live API
#     url = (
#         f"https://management.azure.com/subscriptions/{sub_id}"
#         f"/providers/Microsoft.ResourceHealth/events"
#         f"?api-version=2024-02-01"
#         f"&$filter=eventType eq 'ServiceIssue'"
#     )
#     try:
#         start = time.monotonic()
#         resp = requests.get(
#             url,
#             headers={"Authorization": f"Bearer {token}"},
#             timeout=service.get("timeout", 30),
#         )
#         elapsed_ms = (time.monotonic() - start) * 1000
#         resp.raise_for_status()
#         events = resp.json().get("value", [])
#         active = [e for e in events if e.get("properties", {}).get("status") == "Active"]
#         if active:
#             titles = [e["properties"].get("title", "Unknown") for e in active]
#             return "down", elapsed_ms, f"Active incidents: {'; '.join(titles)}"
#         return "up", elapsed_ms, None
#     except Exception as e:
#         return "down", None, str(e)


# --- Azure Resource Health (per-resource availability) ---
# Config must include resource_id (full ARM resource path).
#
# def check_azure_resource_health(service):
#     credential = _get_azure_credential()
#     token = credential.get_token("https://management.azure.com/.default").token
#     resource_id = service["resource_id"]
#
#     url = (
#         f"https://management.azure.com{resource_id}"
#         f"/providers/Microsoft.ResourceHealth/availabilityStatuses/current"
#         f"?api-version=2024-02-01"
#     )
#     try:
#         start = time.monotonic()
#         resp = requests.get(
#             url,
#             headers={"Authorization": f"Bearer {token}"},
#             timeout=service.get("timeout", 30),
#         )
#         elapsed_ms = (time.monotonic() - start) * 1000
#         resp.raise_for_status()
#         props = resp.json().get("properties", {})
#         # Azure returns: Available, Unavailable, Degraded, Unknown
#         # We map all non-Available to "down" (no degraded state in our model)
#         status = props.get("availabilityState", "Unknown")
#         if status == "Available":
#             return "up", elapsed_ms, None
#         return "down", elapsed_ms, f"Availability: {status}"
#     except Exception as e:
#         return "down", None, str(e)


CHECKERS = {
    "http": check_http,
    "tcp": check_tcp,
    "dns": check_dns,
    "script": check_script,
    # "azure_service_health": check_azure_service_health,
    # "azure_resource_health": check_azure_resource_health,
}


def run_check(service):
    check_type = service.get("type", "http")
    checker = CHECKERS.get(check_type)
    if not checker:
        return "down", None, f"Unknown check type: {check_type}"
    return checker(service)
