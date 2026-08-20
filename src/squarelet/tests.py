"""Tests for python-squarelet"""

import base64
import json
import os
import time

import pytest

from squarelet import CredentialsFailedError, DoesNotExistError, SquareletClient


def _make_expired_token():
    """Build a syntactically valid JWT whose exp is in the past.

    Only the payload segment needs to be real base64url JSON with an `exp`
    claim, since _token_expiring only decodes the payload. The signature is
    never verified client-side, so a placeholder is fine.
    """

    def _b64(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = _b64({"alg": "RS256", "typ": "JWT"})
    payload = _b64({"exp": int(time.time()) - 60, "token_type": "access"})
    return f"{header}.{payload}.signature"

# pylint:disable=redefined-outer-name, protected-access
@pytest.fixture
def squarelet_client():
    """Fixture to mock a SquareletClient instance."""
    sq_user = os.environ.get("SQ_USER")
    sq_password = os.environ.get("SQ_PASSWORD")
    return SquareletClient(
        base_uri="https://api.www.documentcloud.org/api/",
        username=sq_user,
        password=sq_password,
    )


@pytest.fixture
def muckrock_client():
    """Fixture to mock a squarelet client for muckrock api"""
    sq_user = os.environ.get("SQ_USER")
    sq_password = os.environ.get("SQ_PASSWORD")
    return SquareletClient(
        base_uri="https://www.muckrock.com/api_v2/",
        username=sq_user,
        password=sq_password,
    )


def test_get_tokens(squarelet_client):
    """Test token retrieval via username and password."""
    assert squarelet_client.access_token is not None
    assert squarelet_client.refresh_token is not None


def test_get_tokens_invalid_credentials(squarelet_client):
    """Try to authenticate with fake credentials"""
    # pylint:disable = protected-access
    with pytest.raises(CredentialsFailedError):
        squarelet_client._get_tokens("invalid_user", "invalid_pass")


def test_raises_for_status(squarelet_client):
    """Assert that other errors are raised"""
    with pytest.raises(DoesNotExistError) as excinfo:
        # This should raise the DoesNotExistError since the status code will be 404
        squarelet_client.request("get", "documents/0/")
    assert excinfo.value.response.status_code == 404


def test_access_documentcloud(squarelet_client):
    """Test that we can access the DocumentCloud endpoint"""
    sq_user = os.environ.get("SQ_USER")
    my_user = squarelet_client.request("get", "users/me/")
    user_data = my_user.json()
    # We assert here that the username returned by DocumentCloud is our current username
    assert user_data["username"] == sq_user


def test_access_muckrock(muckrock_client):
    """Test that we can access the MuckRock endpoint"""
    sq_user = os.environ.get("SQ_USER")
    my_user = muckrock_client.request("get", "users/me")
    user_data = my_user.json()
    assert user_data["username"] == sq_user


def test_rate_limit(squarelet_client):
    """Test that rate limiting is enforced and requests don't exceed the allowed rate"""

    rate_limit = 10
    rate_period = 1

    start = time.time()

    # Make more requests than the rate limit allows in one period
    for _ in range(rate_limit + 1):
        squarelet_client.request("get", "users/me/")

    elapsed = time.time() - start

    # If rate limiting is working, the 11th request should have been delayed
    # until the next period, so total time should be >= 1 second
    assert elapsed >= rate_period, (
        f"Rate limit not enforced: {rate_limit+ 1} requests completed in {elapsed:.2f}s, "
        f"expected at least {rate_period}s"
    )


def test_refresh_tokens(squarelet_client):
    """Test that tokens can be refreshed using a valid refresh token"""
    original_access = squarelet_client.access_token
    new_access, new_refresh = squarelet_client._refresh_tokens(
        squarelet_client.refresh_token
    )
    assert new_access is not None
    assert new_refresh is not None
    # New access token should differ from the original
    assert new_access != original_access


def test_refresh_tokens_invalid_no_credentials():
    """
    Test that an invalid refresh token raises CredentialsFailedError
    when no credentials are available to fall back on
    """
    no_cred_client = SquareletClient(base_uri="https://api.www.documentcloud.org/api/")
    with pytest.raises(CredentialsFailedError):
        no_cred_client._refresh_tokens("invalid_refresh_token")


def test_refresh_tokens_invalid_falls_back_to_credentials(squarelet_client):
    """Test that an expired/invalid refresh token falls back to credentials to get new tokens"""
    new_access, new_refresh = squarelet_client._refresh_tokens("invalid_refresh_token")
    assert new_access is not None
    assert new_refresh is not None


def test_user_agent_anonymous():
    """Unauthenticated client should have 'Anonymous' in User-Agent"""
    client = SquareletClient(base_uri="https://api.www.documentcloud.org/api/")
    assert "Anonymous" in client.session.headers["User-Agent"]


def test_user_agent_authenticated(squarelet_client):
    """Authenticated client should have username in User-Agent, not 'Anonymous'"""
    ua = squarelet_client.session.headers["User-Agent"]
    sq_user = os.environ.get("SQ_USER")
    assert sq_user in ua
    assert "Anonymous" not in ua

def test_no_credentials_no_tokens():
    """Test that a client without credentials has no tokens set"""
    client = SquareletClient(base_uri="https://api.www.documentcloud.org/api/")
    assert client.access_token is None
    assert client.refresh_token is None

def test_user_id_cached(squarelet_client):
    """Test that user_id is fetched once and cached"""
    first = squarelet_client.user_id
    squarelet_client.session = None  # Would blow up if a request was made
    assert squarelet_client.user_id == first

def test_token_expiring_detects_expired():
    """_token_expiring returns True for a missing or expired token, and
    False for an unparseable (non-JWT) token so we fall back to reactive auth."""
    client = SquareletClient(base_uri="https://api.www.documentcloud.org/api/")
    # No token at all -> expiring
    assert client._token_expiring() is True
    # Expired JWT -> expiring
    client.access_token = _make_expired_token()
    assert client._token_expiring() is True
    # Unparseable token -> not treated as expiring; let the server decide
    client.access_token = "not-a-jwt"
    assert client._token_expiring() is False


def test_proactive_refresh_before_expired_request(squarelet_client):
    """An expired access token should be refreshed proactively, before the
    request is sent, so we never send a known-expired token (which DocumentCloud
    would treat as anonymous and could hit the anonymous 429 quota)."""
    # Force an expired token onto the session while keeping a valid refresh
    # token, mimicking a client that has been idle past the 5-min token TTL.
    expired = _make_expired_token()
    squarelet_client.access_token = expired
    squarelet_client.session.headers.update({"Authorization": f"Bearer {expired}"})

    # _token_expiring must recognize the expired token
    assert squarelet_client._token_expiring() is True

    # The request should succeed because request() refreshes proactively
    resp = squarelet_client.request("get", "users/me/")
    assert resp.status_code == 200

    # The token must have been swapped out for a fresh, non-expired one
    assert squarelet_client.access_token != expired
    assert squarelet_client._token_expiring() is False
    # And the session header must carry the new token, not the expired one
    assert squarelet_client.session.headers["Authorization"] == (
        f"Bearer {squarelet_client.access_token}"
    )
