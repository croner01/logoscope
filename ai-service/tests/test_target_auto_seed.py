"""Tests for target auto-seed logic."""
from unittest.mock import MagicMock, patch

import requests

from ai.target_auto_seed import discover_new_clusters, register_target, run_auto_seed


class TestDiscoverNewClusters:
    def test_discovers_new_cluster_namespace_pairs(self):
        """New source_cluster values produce target candidates."""
        mock_client = MagicMock()
        mock_client.query.return_value.result_rows = [
            ("openstack-cluster-01", "openstack", 5000),
            ("openstack-cluster-01", "kube-system", 100),
            ("cluster-local", "islap", 20000),
        ]

        targets = discover_new_clusters(mock_client)

        assert len(targets) == 3
        ids = [t["target_identity"] for t in targets]
        assert "namespace:openstack/cluster:openstack-cluster-01" in ids
        assert "namespace:kube-system/cluster:openstack-cluster-01" in ids
        assert "namespace:islap/cluster:cluster-local" in ids

    def test_skips_existing_targets(self):
        """Already-registered target identities are not duplicated."""
        mock_client = MagicMock()

        # Two queries: first for new clusters, second for existing targets
        mock_client.query.side_effect = [
            MagicMock(result_rows=[
                ("openstack-cluster-01", "openstack", 5000),
                ("cluster-local", "islap", 20000),
            ]),
            MagicMock(result_rows=[
                ("namespace:islap/cluster:cluster-local",),
            ]),
        ]

        targets = discover_new_clusters(mock_client)

        identities = [t["target_identity"] for t in targets]
        assert "namespace:openstack/cluster:openstack-cluster-01" in identities
        # islap already registered → skipped
        assert "namespace:islap/cluster:cluster-local" not in identities

    def test_skips_legacy_format_targets(self):
        """Existing targets in legacy format (namespace only) also prevent duplicates."""
        mock_client = MagicMock()
        mock_client.query.side_effect = [
            MagicMock(result_rows=[
                ("cluster-local", "islap", 20000),
            ]),
            MagicMock(result_rows=[
                ("namespace:islap",),  # legacy format
            ]),
        ]

        targets = discover_new_clusters(mock_client)
        assert len(targets) == 0

    def test_empty_when_no_source_cluster_data(self):
        """When no logs have source_cluster set, no targets are discovered."""
        mock_client = MagicMock()
        mock_client.query.return_value.result_rows = []

        targets = discover_new_clusters(mock_client)
        assert len(targets) == 0

    def test_returns_empty_when_all_already_registered(self):
        """When all (cluster, namespace) pairs already have targets, nothing new."""
        mock_client = MagicMock()
        mock_client.query.side_effect = [
            MagicMock(result_rows=[
                ("cluster-a", "ns1", 100),
                ("cluster-b", "ns2", 200),
            ]),
            MagicMock(result_rows=[
                ("namespace:ns1/cluster:cluster-a",),
                ("namespace:ns2/cluster:cluster-b",),
            ]),
        ]

        targets = discover_new_clusters(mock_client)
        assert len(targets) == 0


class TestRegisterTarget:
    def test_register_success(self):
        """Successful API call returns True."""
        target = {"target_identity": "namespace:test/cluster:test-cluster"}
        with patch("ai.target_auto_seed.requests.post") as mock_post:
            mock_post.return_value.status_code = 201
            result = register_target("http://ai:8090", target)
            assert result is True
            mock_post.assert_called_once()

    def test_register_failure_logged(self):
        """API error returns False without raising."""
        target = {"target_identity": "namespace:test/cluster:test-cluster"}
        with patch("ai.target_auto_seed.requests.post") as mock_post:
            mock_post.return_value.status_code = 409
            result = register_target("http://ai:8090", target)
            assert result is False

    def test_register_network_error(self):
        """Network error returns False without raising."""
        target = {"target_identity": "namespace:test/cluster:test-cluster"}
        with patch("ai.target_auto_seed.requests.post") as mock_post:
            mock_post.side_effect = requests.RequestException("connection refused")
            result = register_target("http://ai:8090", target)
            assert result is False


class TestRunAutoSeed:
    def test_integration_happy_path(self):
        """run_auto_seed returns count of newly registered targets."""
        mock_client = MagicMock()
        mock_client.query.return_value.result_rows = [
            ("cluster-x", "ns-x", 50),
        ]

        with patch("ai.target_auto_seed.clickhouse_connect.get_client", return_value=mock_client):
            with patch("ai.target_auto_seed.register_target", return_value=True) as mock_register:
                count = run_auto_seed()
                assert count == 1
                mock_register.assert_called_once()

    def test_counts_only_successful_registrations(self):
        """Failed registrations are not counted."""
        mock_client = MagicMock()
        mock_client.query.side_effect = [
            MagicMock(result_rows=[
                ("cluster-x", "ns-x", 50),
                ("cluster-y", "ns-y", 30),
            ]),
            MagicMock(result_rows=[]),  # no existing targets
        ]

        with patch("ai.target_auto_seed.clickhouse_connect.get_client", return_value=mock_client):
            with patch("ai.target_auto_seed.register_target", side_effect=[True, False]):
                count = run_auto_seed()
                assert count == 1  # only first succeeded
