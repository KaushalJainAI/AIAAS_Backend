"""
Secret references (`credentials/refs.py`): name a credential, never contain one.

Pinned: a reference resolves only for its owner's credential, only for a slug
the calling tool allows, a blob field wins over a same-named token column, and
resolved values are scrubbed from results before the model sees them.
"""
from __future__ import annotations

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase

from credentials.manager import CredentialManager
from credentials.models import Credential, CredentialType
from credentials.refs import SecretRefError, aresolve_refs, find_refs, redact

User = get_user_model()


class SecretRefTests(TestCase):
    def setUp(self):
        CredentialManager()._cache.clear()
        self.user = User.objects.create_user(username='refer', password='pw')
        self.other = User.objects.create_user(username='stranger', password='pw')
        self.cred_type, _ = CredentialType.objects.update_or_create(
            slug='portal-gst',
            defaults={'name': 'GST Portal', 'auth_method': 'custom',
                      'fields_schema': [{'name': 'password'}]},
        )
        self.cred = Credential(
            user=self.user, credential_type=self.cred_type, name='GST',
        )
        self.cred.set_credential_data({'password': 's3cr3t-pw', 'username': 'u1'})
        self.cred.save()

    def tearDown(self):
        CredentialManager()._cache.clear()

    def test_dict_form_resolves(self):
        out = async_to_sync(aresolve_refs)(
            {'password': {'secret_ref': 'portal-gst.password'}},
            self.user.id, {'portal-gst'},
        )
        self.assertEqual(out, {'password': 's3cr3t-pw'})

    def test_string_form_interpolates(self):
        out = async_to_sync(aresolve_refs)(
            {'url': 'https://u:{{secret:portal-gst.password}}@portal/login'},
            self.user.id, {'portal-gst'},
        )
        self.assertEqual(out, {'url': 'https://u:s3cr3t-pw@portal/login'})

    def test_find_refs_lists_both_forms(self):
        refs = find_refs({
            'a': {'secret_ref': 'portal-gst.password'},
            'b': ['x {{secret:portal-gst.username}} y'],
        })
        self.assertEqual(refs, [('portal-gst', 'password'), ('portal-gst', 'username')])

    def test_slug_outside_the_allowlist_is_refused(self):
        with self.assertRaises(SecretRefError):
            async_to_sync(aresolve_refs)(
                {'password': {'secret_ref': 'portal-gst.password'}},
                self.user.id, {'gmail'},
            )

    def test_another_users_credential_does_not_resolve(self):
        with self.assertRaises(SecretRefError):
            async_to_sync(aresolve_refs)(
                {'password': {'secret_ref': 'portal-gst.password'}},
                self.other.id, {'portal-gst'},
            )

    def test_unknown_field_is_refused(self):
        with self.assertRaises(SecretRefError):
            async_to_sync(aresolve_refs)(
                {'x': {'secret_ref': 'portal-gst.nope'}},
                self.user.id, {'portal-gst'},
            )

    def test_args_without_refs_pass_through_untouched(self):
        args = {'a': 1, 'b': ['x']}
        out = async_to_sync(aresolve_refs)(args, self.user.id, {'portal-gst'})
        self.assertEqual(out, args)

    def test_redact_scrubs_the_value_but_keeps_the_shape(self):
        self.assertEqual(
            redact('login failed with s3cr3t-pw for u1', ['s3cr3t-pw']),
            'login failed with [redacted] for u1',
        )
        # Short fragments are not secrets; scrubbing them mangles real text.
        self.assertEqual(redact('abc here', ['abc']), 'abc here')
