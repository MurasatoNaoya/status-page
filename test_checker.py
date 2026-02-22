"""Tests for checker.py — health check functions."""

from unittest.mock import patch, MagicMock
import socket

from checker import (
    check_http,
    check_tcp,
    check_dns,
    check_script,
    check_dns_bar,
    run_check,
)


class TestCheckHTTP:
    def test_skips_when_required_env_missing(self):
        status, ms, err = check_http(
            {
                "url": "https://example.com",
                "requires_env": "ON_PRIVATE_NETWORK",
            }
        )
        assert status == "skip"
        assert ms is None
        assert "requires env ON_PRIVATE_NETWORK" in err

    def test_successful_check(self):
        with patch("checker.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_get.return_value = mock_resp
            status, ms, err = check_http(
                {"url": "https://example.com", "expected_status": 200}
            )
            assert status == "up"
            assert ms is not None
            assert err is None

    def test_wrong_status_code(self):
        with patch("checker.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_resp.text = "Internal Server Error"
            mock_get.return_value = mock_resp
            status, ms, err = check_http({"url": "https://example.com"})
            assert status == "down"
            assert "HTTP 500" in err

    def test_rate_limit_treated_as_up(self):
        with patch("checker.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 403
            mock_resp.text = "API rate limit exceeded"
            mock_get.return_value = mock_resp
            status, ms, err = check_http({"url": "https://api.github.com"})
            assert status == "up"

    def test_rate_limit_429(self):
        with patch("checker.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 429
            mock_resp.text = "rate limit"
            mock_get.return_value = mock_resp
            status, ms, err = check_http({"url": "https://api.github.com"})
            assert status == "up"

    def test_connection_error(self):
        import requests as req

        with patch("checker.requests.get") as mock_get:
            mock_get.side_effect = req.RequestException("Connection refused")
            status, ms, err = check_http({"url": "https://example.com"})
            assert status == "down"
            assert ms is None
            assert "Connection refused" in err

    def test_auth_token_env(self):
        with (
            patch("checker.requests.get") as mock_get,
            patch.dict("os.environ", {"MY_TOKEN": "secret123"}),
        ):
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_get.return_value = mock_resp
            check_http({"url": "https://api.example.com", "auth_token_env": "MY_TOKEN"})
            call_kwargs = mock_get.call_args
            assert call_kwargs[1]["headers"]["Authorization"] == "token secret123"

    def test_custom_expected_status(self):
        with patch("checker.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 401
            mock_get.return_value = mock_resp
            status, ms, err = check_http(
                {"url": "https://example.com", "expected_status": 401}
            )
            assert status == "up"


class TestCheckTCP:
    def test_skips_when_required_env_missing(self):
        status, ms, err = check_tcp(
            {"host": "localhost", "port": 5555, "requires_env": "ON_PRIVATE_NETWORK"}
        )
        assert status == "skip"
        assert ms is None
        assert "requires env ON_PRIVATE_NETWORK" in err

    def test_successful_connection(self):
        with patch("checker.socket.create_connection") as mock_conn:
            mock_sock = MagicMock()
            mock_conn.return_value = mock_sock
            status, ms, err = check_tcp({"host": "localhost", "port": 5555})
            assert status == "up"
            mock_sock.close.assert_called_once()

    def test_connection_refused(self):
        with patch("checker.socket.create_connection") as mock_conn:
            mock_conn.side_effect = OSError("Connection refused")
            status, ms, err = check_tcp({"host": "localhost", "port": 9999})
            assert status == "down"
            assert "Connection refused" in err


class TestCheckDNS:
    def test_skips_when_required_env_missing(self):
        status, ms, err = check_dns(
            {"hostname": "example.com", "requires_env": "ON_PRIVATE_NETWORK"}
        )
        assert status == "skip"
        assert ms is None
        assert "requires env ON_PRIVATE_NETWORK" in err

    def test_successful_resolution(self):
        with patch("checker.socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [("AF_INET", None, None, None, ("1.2.3.4", 0))]
            status, ms, err = check_dns({"hostname": "example.com"})
            assert status == "up"
            assert err is None

    def test_resolution_failure(self):
        with patch("checker.socket.getaddrinfo") as mock_dns:
            mock_dns.side_effect = socket.gaierror("Name resolution failed")
            status, ms, err = check_dns({"hostname": "nonexistent.invalid"})
            assert status == "down"
            assert "Name resolution" in err


class TestCheckScript:
    def test_successful_script(self):
        status, ms, err = check_script({"command": "true", "timeout": 5})
        assert status == "up"
        assert err is None

    def test_failing_script(self):
        status, ms, err = check_script({"command": "false", "timeout": 5})
        assert status == "down"

    def test_script_timeout(self):
        status, ms, err = check_script({"command": "sleep 10", "timeout": 1})
        assert status == "down"
        assert "timed out" in err.lower()


class TestCheckDNSBar:
    def test_skips_targets_when_required_env_missing(self):
        results = check_dns_bar(
            [
                {
                    "hostname": "private.example",
                    "label": "Private",
                    "requires_env": "ON_PRIVATE_NETWORK",
                }
            ]
        )
        assert len(results) == 1
        assert results[0]["status"] == "skip"
        assert "requires env ON_PRIVATE_NETWORK" in (results[0]["error"] or "")

    def test_all_targets_up(self):
        with patch("checker.socket.getaddrinfo") as mock_dns:
            mock_dns.return_value = [("AF_INET", None, None, None, ("1.2.3.4", 0))]
            results = check_dns_bar(
                [
                    {"hostname": "example.com", "label": "Example"},
                    {"hostname": "test.com", "label": "Test"},
                ]
            )
            assert len(results) == 2
            assert all(r["status"] == "up" for r in results)
            assert all(r["ms"] is not None for r in results)

    def test_mixed_results(self):
        def side_effect(hostname, _):
            if hostname == "bad.com":
                raise socket.gaierror("failed")
            return [("AF_INET", None, None, None, ("1.2.3.4", 0))]

        with patch("checker.socket.getaddrinfo", side_effect=side_effect):
            results = check_dns_bar(
                [
                    {"hostname": "good.com", "label": "Good"},
                    {"hostname": "bad.com", "label": "Bad"},
                ]
            )
            assert results[0]["status"] == "up"
            assert results[1]["status"] == "down"
            assert results[1]["error"] is not None


class TestRunCheck:
    def test_unknown_type(self):
        status, ms, err = run_check({"type": "foobar"})
        assert status == "down"
        assert "Unknown" in err

    def test_defaults_to_http(self):
        with patch("checker.requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_get.return_value = mock_resp
            status, ms, err = run_check({"url": "https://example.com"})
            assert status == "up"
