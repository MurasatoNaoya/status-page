"""Config validation for config.yaml."""

from urllib.parse import urlparse

_SERVICE_TYPES = {
    "http",
    "tcp",
    "dns",
    "script",
    "azure_service_health",
    "azure_resource_health",
}
_FEED_TYPES = {"statuspage", "statusio", "azure_rss", "azure_service_health"}


def _require_dict(value, field):
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be a mapping")
    return value


def _require_list(value, field):
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return value


def _require_str(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _require_int(value, field, minimum=1, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} must be an integer <= {maximum}")
    return value


def _require_url_http(value, field):
    url = _require_str(value, field)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"{field} must be a valid http(s) URL")
    return url


def _validate_service(service, field):
    svc = _require_dict(service, field)
    _require_str(svc.get("name"), f"{field}.name")
    stype = svc.get("type", "http")
    _require_str(stype, f"{field}.type")
    if stype not in _SERVICE_TYPES:
        raise ValueError(
            f"{field}.type must be one of: {', '.join(sorted(_SERVICE_TYPES))}"
        )
    if "interval" in svc:
        _require_int(svc["interval"], f"{field}.interval")
    if "timeout" in svc:
        _require_int(svc["timeout"], f"{field}.timeout")
    if "requires_env" in svc:
        _require_str(svc["requires_env"], f"{field}.requires_env")

    if stype == "http":
        _require_url_http(svc.get("url"), f"{field}.url")
        if "expected_status" in svc:
            _require_int(svc["expected_status"], f"{field}.expected_status", 100, 599)
        if "auth_token_env" in svc:
            _require_str(svc["auth_token_env"], f"{field}.auth_token_env")
    elif stype == "tcp":
        _require_str(svc.get("host"), f"{field}.host")
        _require_int(svc.get("port"), f"{field}.port", 1, 65535)
    elif stype == "dns":
        _require_str(svc.get("hostname"), f"{field}.hostname")
    elif stype == "script":
        cmd = svc.get("command")
        if isinstance(cmd, list):
            if not cmd:
                raise ValueError(f"{field}.command must not be empty")
            for i, part in enumerate(cmd):
                _require_str(part, f"{field}.command[{i}]")
        else:
            _require_str(cmd, f"{field}.command")
    elif stype == "azure_resource_health":
        _require_str(svc.get("resource_id"), f"{field}.resource_id")
    elif stype == "azure_service_health":
        if "subscription_id" in svc:
            _require_str(svc["subscription_id"], f"{field}.subscription_id")
    return svc


def _validate_status_feed(feed, field):
    item = _require_dict(feed, field)
    _require_str(item.get("name"), f"{field}.name")
    ftype = item.get("type", "statuspage")
    _require_str(ftype, f"{field}.type")
    if ftype not in _FEED_TYPES:
        raise ValueError(
            f"{field}.type must be one of: {', '.join(sorted(_FEED_TYPES))}"
        )
    if ftype != "azure_service_health":
        _require_url_http(item.get("url"), f"{field}.url")
    if "history_url" in item:
        _require_url_http(item["history_url"], f"{field}.history_url")
    if "interval" in item:
        _require_int(item["interval"], f"{field}.interval")
    if "max_incident_pages" in item:
        _require_int(item["max_incident_pages"], f"{field}.max_incident_pages", 1, 100)
    if "components" in item:
        comps = _require_dict(item["components"], f"{field}.components")
        for key, value in comps.items():
            _require_str(key, f"{field}.components key")
            _require_str(value, f"{field}.components[{key}]")
    if "covered_services" in item:
        covered = _require_list(item["covered_services"], f"{field}.covered_services")
        for i, svc in enumerate(covered):
            _require_str(svc, f"{field}.covered_services[{i}]")
    if "exclude_regions" in item:
        regions = _require_list(item["exclude_regions"], f"{field}.exclude_regions")
        for i, region in enumerate(regions):
            _require_str(region, f"{field}.exclude_regions[{i}]")
    if "backfill_cap_days" in item:
        _require_int(item["backfill_cap_days"], f"{field}.backfill_cap_days", 1)
    if "backfill_cap_summary" in item:
        _require_str(item["backfill_cap_summary"], f"{field}.backfill_cap_summary")
    if ftype == "azure_service_health":
        _require_str(item.get("subscription_id"), f"{field}.subscription_id")
    return item


def validate_config(config):
    cfg = _require_dict(config, "config")

    page = cfg.get("page", {})
    if page:
        page = _require_dict(page, "page")
        if "title" in page:
            _require_str(page["title"], "page.title")
        if "description" in page:
            _require_str(page["description"], "page.description")

    feeds = _require_list(cfg.get("status_feeds", []), "status_feeds")
    for i, feed in enumerate(feeds):
        _validate_status_feed(feed, f"status_feeds[{i}]")

    services = _require_list(cfg.get("services", []), "services")
    for i, svc in enumerate(services):
        _validate_service(svc, f"services[{i}]")

    groups = _require_list(cfg.get("groups", []), "groups")
    for i, group in enumerate(groups):
        grp = _require_dict(group, f"groups[{i}]")
        _require_str(grp.get("name"), f"groups[{i}].name")
        group_services = _require_list(grp.get("services", []), f"groups[{i}].services")
        for j, svc in enumerate(group_services):
            _validate_service(svc, f"groups[{i}].services[{j}]")

    dns_bar = cfg.get("dns_bar")
    if dns_bar is not None:
        dns = _require_dict(dns_bar, "dns_bar")
        if "name" in dns:
            _require_str(dns["name"], "dns_bar.name")
        if "interval" in dns:
            _require_int(dns["interval"], "dns_bar.interval")
        if "requires_env" in dns:
            _require_str(dns["requires_env"], "dns_bar.requires_env")
        targets = _require_list(dns.get("targets", []), "dns_bar.targets")
        for i, target in enumerate(targets):
            tgt = _require_dict(target, f"dns_bar.targets[{i}]")
            _require_str(tgt.get("hostname"), f"dns_bar.targets[{i}].hostname")
            if "label" in tgt:
                _require_str(tgt["label"], f"dns_bar.targets[{i}].label")
            if "requires_env" in tgt:
                _require_str(tgt["requires_env"], f"dns_bar.targets[{i}].requires_env")

    return cfg
