"""
Unit tests for executor/credential_utils.py — focused on the selection logic
(which credential IDs get pulled), not the actual decryption path.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from executor.credential_utils import get_workflow_credentials


class GetWorkflowCredentialsTests(SimpleTestCase):
    def test_empty_workflow_returns_empty(self):
        self.assertEqual(get_workflow_credentials(user_id=1, workflow_json={}), {})

    def test_no_credential_references_returns_empty(self):
        wf = {"nodes": [{"data": {"nodeType": "code", "config": {"code": "pass"}}}]}
        self.assertEqual(get_workflow_credentials(user_id=1, workflow_json=wf), {})

    def test_only_numeric_ids_queried(self):
        wf = {"nodes": [
            {"data": {"nodeType": "http_request", "config": {"credential_id": "42"}}},
            # Non-numeric ID (legacy "cred_foo" style) must not hit the DB.
            {"data": {"nodeType": "slack", "config": {"credential_id": "cred_foo"}}},
        ]}
        with patch("executor.credential_utils.Credential.objects") as objs:
            filter_mock = MagicMock()
            objs.filter.return_value = filter_mock
            filter_mock.select_related.return_value = []
            get_workflow_credentials(user_id=1, workflow_json=wf)
            # Single filter call; id__in contains only the numeric "42".
            kwargs = objs.filter.call_args.kwargs
            self.assertEqual(kwargs.get("id__in"), ["42"])

    def test_recognises_all_credential_key_aliases(self):
        # credential_id, credentialId, credential should all trigger lookup.
        wf = {"nodes": [
            {"data": {"nodeType": "a", "config": {"credential_id": "1"}}},
            {"data": {"nodeType": "b", "config": {"credentialId": "2"}}},
            {"data": {"nodeType": "c", "config": {"credential": "3"}}},
        ]}
        with patch("executor.credential_utils.Credential.objects") as objs:
            filter_mock = MagicMock()
            objs.filter.return_value = filter_mock
            filter_mock.select_related.return_value = []
            get_workflow_credentials(user_id=1, workflow_json=wf)
            self.assertEqual(
                set(objs.filter.call_args.kwargs.get("id__in")), {"1", "2", "3"},
            )
