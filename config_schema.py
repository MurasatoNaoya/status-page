"""Config validation for config.yaml."""


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


def _require_int(value, field, minimum=1):
    if not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _validate_service(service, field):
    svc = _require_dict(service, field)
    _require_str(svc.get("name"), f"{field}.name")
    stype = svc.get("type", "http")
    _require_str(stype, f"{field}.type")
    if "interval" in svc:
        _require_int(svc["interval"], f"{field}.interval")
    return svc


def _validate_status_feed(feed, field):
    item = _require_dict(feed, field)
    _require_str(item.get("name"), f"{field}.name")
    _require_str(item.get("url"), f"{field}.url")
    if "interval" in item:
        _require_int(item["interval"], f"{field}.interval")
    if "components" in item and not isinstance(item["components"], dict):
        raise ValueError(f"{field}.components must be a mapping")
    if "covered_services" in item:
        covered = _require_list(item["covered_services"], f"{field}.covered_services")
        for i, svc in enumerate(covered):
            _require_str(svc, f"{field}.covered_services[{i}]")
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

    feeds = cfg.get("status_feeds", [])
    feeds = _require_list(feeds, "status_feeds")
    for i, feed in enumerate(feeds):
        _validate_status_feed(feed, f"status_feeds[{i}]")

    services = cfg.get("services", [])
    services = _require_list(services, "services")
    for i, svc in enumerate(services):
        _validate_service(svc, f"services[{i}]")

    groups = cfg.get("groups", [])
    groups = _require_list(groups, "groups")
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
        targets = _require_list(dns.get("targets", []), "dns_bar.targets")
        for i, target in enumerate(targets):
            tgt = _require_dict(target, f"dns_bar.targets[{i}]")
            _require_str(tgt.get("hostname"), f"dns_bar.targets[{i}].hostname")
            if "label" in tgt:
                _require_str(tgt["label"], f"dns_bar.targets[{i}].label")

    return cfg
