"""
Shared fixtures for integration tests.

Force the in-memory test settings module so pytest-django builds a SQLite
test DB without touching the project's local Postgres / Redis.
"""
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "workflow_backend.settings.test")

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

User = get_user_model()


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def user(db):
    return User.objects.create_user(
        username="alice",
        email="alice@example.com",
        password="Sup3r$ecret!",
    )


@pytest.fixture
def other_user(db):
    return User.objects.create_user(
        username="mallory",
        email="mallory@example.com",
        password="Sup3r$ecret!",
    )


@pytest.fixture
def auth_client(api_client, user):
    api_client.force_authenticate(user=user)
    return api_client
