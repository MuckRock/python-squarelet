""" Tests for python-squarelet """

import os
import pytest
from squarelet import CredentialsFailedError, DoesNotExistError, SquareletClient


# pylint:disable=redefined-outer-name
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
        squarelet_client.request("get", "blank")
    assert excinfo.value.response.status_code == 404


def test_access_documentcloud(squarelet_client):
    """Test that we can access the DocumentCloud endpoint"""
    sq_user = os.environ.get("SQ_USER")
    my_user = squarelet_client.request("get", "users/me/")
    user_data = my_user.json()
    # We assert here that the username returned by DocumentCloud is our current username
    assert user_data["username"] == sq_user


## TO DO def test_access_muckrock():
## TO DO def test_rate_limit(squarelet_client):
