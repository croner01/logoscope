"""Tests for the hosts management API endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest


class TestListHosts:
    def test_list_hosts_empty(self, client):
        with patch("api.hosts.list_hosts", return_value=[]):
            resp = client.get("/hosts")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_hosts_with_data(self, client):
        hosts = [
            {
                "name": "node-3",
                "host": "10.0.0.3",
                "port": 22,
                "user": "root",
                "key_file": "",
                "labels": {},
                "created_at": "2026-05-30 00:00:00.000",
                "updated_at": "2026-05-30 00:00:00.000",
            },
        ]
        with patch("api.hosts.list_hosts", return_value=hosts):
            resp = client.get("/hosts")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["name"] == "node-3"
        assert data[0]["host"] == "10.0.0.3"


class TestGetHost:
    def test_get_existing_host(self, client):
        host = {
            "name": "node-3",
            "host": "10.0.0.3",
            "port": 22,
            "user": "root",
            "key_file": "",
            "labels": {},
            "created_at": "2026-05-30 00:00:00.000",
            "updated_at": "2026-05-30 00:00:00.000",
        }
        with patch("api.hosts.get_host", return_value=host):
            resp = client.get("/hosts/node-3")
        assert resp.status_code == 200
        assert resp.json()["name"] == "node-3"

    def test_get_nonexistent_host(self, client):
        with patch("api.hosts.get_host", return_value=None):
            resp = client.get("/hosts/nonexistent")
        assert resp.status_code == 404
        assert "not found" in resp.text


class TestRegisterHost:
    def test_register_host(self, client):
        req = {
            "name": "node-5",
            "host": "10.0.0.5",
            "port": 22,
            "user": "admin",
            "key_file": "/etc/ssh-keys/admin/id_rsa",
            "labels": {"region": "us-east-1"},
        }
        with patch("api.hosts.register_host") as mock_register:
            mock_register.return_value = {
                **req,
                "labels_json": '{"region": "us-east-1"}',
                "created_at": "2026-05-30 00:00:00.000",
                "updated_at": "2026-05-30 00:00:00.000",
            }
            resp = client.post("/hosts", json=req)

        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "node-5"
        assert data["labels"] == {"region": "us-east-1"}

    def test_register_host_missing_name(self, client):
        resp = client.post("/hosts", json={"host": "10.0.0.5"})
        assert resp.status_code == 422  # validation error

    def test_register_host_missing_host(self, client):
        resp = client.post("/hosts", json={"name": "node-5"})
        assert resp.status_code == 422


class TestUnregisterHost:
    def test_unregister_host(self, client):
        with patch("api.hosts.unregister_host", return_value=True):
            resp = client.delete("/hosts/node-3")
        assert resp.status_code == 204

    def test_unregister_nonexistent_returns_500(self, client):
        with patch("api.hosts.unregister_host", return_value=False):
            resp = client.delete("/hosts/nonexistent")
        assert resp.status_code == 500


class TestValidation:
    def test_invalid_port(self, client):
        resp = client.post(
            "/hosts",
            json={"name": "n1", "host": "10.0.0.1", "port": 0},
        )
        assert resp.status_code == 422

    def test_invalid_port_too_large(self, client):
        resp = client.post(
            "/hosts",
            json={"name": "n1", "host": "10.0.0.1", "port": 65536},
        )
        assert resp.status_code == 422
