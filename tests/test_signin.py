"""One account per email (cloud/signin/link.py, Cognito's pre sign-up trigger), against a fake Cognito."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("boto3")

ROOT = Path(__file__).resolve().parent.parent


class FakeCognito:
    def __init__(self, users):
        self.users, self.links = users, []

    def list_users(self, UserPoolId, Filter):
        email = Filter.split('"')[1]
        return {"Users": [u for u in self.users if u["email"] == email]}

    def admin_link_provider_for_user(self, **kw):
        self.links.append(kw)


def user(name, email, status="CONFIRMED", verified=True):
    return {"Username": name, "UserStatus": status, "email": email,
            "Attributes": [{"Name": "email", "Value": email}, {"Name": "email_verified", "Value": str(verified).lower()}]}


@pytest.fixture
def link(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    spec = importlib.util.spec_from_file_location("link", ROOT / "cloud" / "signin" / "link.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def event(trigger, name, email, verified="true"):
    return {"userPoolId": "pool", "triggerSource": trigger, "userName": name,
            "request": {"userAttributes": {"email": email, "email_verified": verified}}}


def test_google_sign_in_joins_the_password_account_with_that_email(link):
    link.COGNITO = FakeCognito([user("abc-123", "sam@example.com")])
    e = event("PreSignUp_ExternalProvider", "google_998877", "Sam@Example.com")
    assert link.handler(e, None) is e
    assert link.COGNITO.links == [{
        "UserPoolId": "pool",
        "DestinationUser": {"ProviderName": "Cognito", "ProviderAttributeValue": "abc-123"},
        "SourceUser": {"ProviderName": "Google", "ProviderAttributeName": "Cognito_Subject", "ProviderAttributeValue": "998877"}}]


def test_unverified_emails_are_never_joined(link):
    link.COGNITO = FakeCognito([user("abc-123", "sam@example.com", status="UNCONFIRMED", verified=False)])
    link.handler(event("PreSignUp_ExternalProvider", "google_1", "sam@example.com"), None)
    link.COGNITO = FakeCognito([user("abc-123", "sam@example.com")])
    link.handler(event("PreSignUp_ExternalProvider", "google_1", "sam@example.com", verified="false"), None)
    assert link.COGNITO.links == []


def test_a_new_google_email_gets_its_own_account(link):
    link.COGNITO = FakeCognito([user("abc-123", "someone@example.com")])
    link.handler(event("PreSignUp_ExternalProvider", "google_1", "new@example.com"), None)
    assert link.COGNITO.links == []


def test_a_password_account_for_a_google_email_is_refused(link):
    link.COGNITO = FakeCognito([user("google_1", "sam@example.com", status="EXTERNAL_PROVIDER")])
    with pytest.raises(Exception, match="Sign in with Google"):
        link.handler(event("PreSignUp_SignUp", "sam@example.com", "sam@example.com", verified="false"), None)
    link.COGNITO = FakeCognito([])
    e = event("PreSignUp_SignUp", "x@example.com", "x@example.com", verified="false")
    assert link.handler(e, None) is e
