"""Cognito pre sign-up trigger: one account per email, however someone signs in.

- Signing in with Google for the first time, with the email of an existing password account:
  the Google sign-in is linked to that account, so it is the same person (same songs, same
  admin rights) either way. Both emails must be verified (Google's, and the code Cognito sent).
- Making a password account with an email that already signs in with Google only: refused with
  a message to use Google, because Cognito can't merge an existing Google account into a new one.
"""
from __future__ import annotations

import boto3

COGNITO = boto3.client("cognito-idp")
PROVIDERS = {"google": "Google"}  # Cognito user names for Google sign-ins look like "google_1234…"
USE_GOOGLE = "This email already signs in with Google. Go back and choose Sign in with Google."


def handler(event, context):
    pool, trigger = event["userPoolId"], event["triggerSource"]
    attrs = event["request"].get("userAttributes") or {}
    email = (attrs.get("email") or "").strip()
    if not email:
        return event
    if trigger == "PreSignUp_ExternalProvider":
        prefix, _, subject = event["userName"].partition("_")
        provider = PROVIDERS.get(prefix.lower())
        if not provider or str(attrs.get("email_verified")).lower() != "true":
            return event
        for user in same_email(pool, email):
            if user.get("UserStatus") != "EXTERNAL_PROVIDER" and verified(user):
                COGNITO.admin_link_provider_for_user(
                    UserPoolId=pool,
                    DestinationUser={"ProviderName": "Cognito", "ProviderAttributeValue": user["Username"]},
                    SourceUser={"ProviderName": provider, "ProviderAttributeName": "Cognito_Subject",
                                "ProviderAttributeValue": subject})
                break
    elif trigger == "PreSignUp_SignUp":
        if any(u.get("UserStatus") == "EXTERNAL_PROVIDER" for u in same_email(pool, email)):
            raise Exception(USE_GOOGLE)
    return event


def same_email(pool: str, email: str) -> list[dict]:
    """Users with this email, matching any capitalisation (the filter itself is exact)."""
    found = {}
    for form in {email, email.lower()}:
        safe = form.replace("\\", "").replace('"', "")
        for user in COGNITO.list_users(UserPoolId=pool, Filter=f'email = "{safe}"').get("Users", []):
            found[user["Username"]] = user
    return list(found.values())


def verified(user: dict) -> bool:
    return any(a["Name"] == "email_verified" and a["Value"].lower() == "true" for a in user.get("Attributes", []))
