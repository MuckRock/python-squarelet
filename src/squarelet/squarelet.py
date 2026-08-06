"""python-squarelet handles authentication and requests to MuckRock services"""

# Standard Library
import logging
from functools import partial

# Third Party
import ratelimit
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Local
from .exceptions import APIError, CredentialsFailedError, DoesNotExistError

logger = logging.getLogger("squarelet")

BULK_LIMIT = 25
TIMEOUT = 20
RATE_LIMIT = 10
RATE_PERIOD = 1

DEFAULT_AUTH_URI = "https://accounts.muckrock.com/api/"


# pylint: disable=too-many-instance-attributes
class SquareletClient:
    """Handles token auth and requests"""

    # pylint: disable=too-many-arguments, too-many-positional-arguments
    def __init__(
        self,
        base_uri,
        username=None,
        password=None,
        auth_uri=None,
        timeout=TIMEOUT,
        rate_limit=True,
        rate_limit_sleep=True,
    ):
        self.username = username
        self.password = password
        self.base_uri = base_uri
        self.auth_uri = auth_uri or DEFAULT_AUTH_URI
        self.timeout = timeout
        self.session = requests.Session()
        self.access_token = None
        self.refresh_token = None
        self._user_id = None
        self._username_cache = None
        self._resolving_identity = False

        # Capture the library default UA once, before appending any identity,
        # so the identity segment can be rebuilt deterministically later.
        self._base_ua = self.session.headers.get("User-Agent", "")
        self._set_user_agent()
        self._set_tokens()

        # Apply rate limiting
        if rate_limit:
            # Apply rate limit decorator
            self.request = ratelimit.limits(calls=RATE_LIMIT, period=RATE_PERIOD)(
                self.request
            )

            # Apply sleep_and_retry if rate_limit_sleep is enabled
            if rate_limit_sleep:
                self.request = ratelimit.sleep_and_retry(self.request)

    def _fetch_me(self):
        """Fetch the current user record from the API (used for identity/UA)."""
        # set_tokens=False so a 403/429 here can't re-enter _set_tokens and loop.
        return self.request("get", "users/me/", set_tokens=False).json()

    def _resolve_identity(self):
        """Label for the UA: local username, cached value, else resolve from API.

        Token-authed clients (constructed without a username) have no local
        identity, so the only source of truth is the token itself. We ask the
        API who it belongs to and cache the result.
        """
        if self.username:
            return self.username
        if self._username_cache is not None:
            return self._username_cache
        # Only hit the API if we're authenticated and not already resolving.
        if not self.access_token or self._resolving_identity:
            return None
        self._resolving_identity = True
        try:
            data = self._fetch_me()
            # Cache the user id too, so the user_id property doesn't re-fetch.
            if self._user_id is None and data.get("id") is not None:
                self._user_id = data["id"]
            self._username_cache = data.get("username") or str(data.get("id"))
            return self._username_cache
        except Exception:  # pylint: disable=broad-except
            # UA labeling is cosmetic and must never break a real request.
            return None
        finally:
            self._resolving_identity = False

    def _set_user_agent(self):
        """Rebuild the User-Agent from the captured base plus current identity."""
        identity = self._resolve_identity() or "Anonymous"
        self.session.headers.update(
            {"User-Agent": f"{self._base_ua} {identity}".strip()}
        )

    def _set_tokens(self):
        """Set the refresh and access tokens"""
        if self.refresh_token:
            self.access_token, self.refresh_token = self._refresh_tokens(
                self.refresh_token
            )
        elif self.username and self.password:
            self.access_token, self.refresh_token = self._get_tokens(
                self.username, self.password
            )
        else:
            self.access_token = None
            self.refresh_token = None

        if self.access_token:
            self.session.headers.update(
                {"Authorization": f"Bearer {self.access_token}"}
            )

        # Rebuild the UA whenever auth state changes. This works for
        # username/password clients AND token/refresh clients, since it can
        # resolve identity from the API when no local username is available.
        self._set_user_agent()

    def _get_tokens(self, username, password):
        """Get an access and refresh token in exchange for the username and password"""
        response = self.requests_retry_session().post(
            f"{self.auth_uri}token/",
            json={"username": username, "password": password},
            timeout=self.timeout,
        )

        if response.status_code == 401:
            raise CredentialsFailedError("The username and password are incorrect")

        self.raise_for_status(response)

        json = response.json()
        return (json["access"], json["refresh"])

    def _refresh_tokens(self, refresh_token):
        """Refresh the access and refresh tokens"""
        response = self.requests_retry_session().post(
            f"{self.auth_uri}refresh/",
            json={"refresh": refresh_token},
            timeout=self.timeout,
        )

        if response.status_code == 401:
            if not self.username or not self.password:
                raise CredentialsFailedError(
                    "Refresh token expired and no credentials available to re-authenticate"
                )
            return self._get_tokens(self.username, self.password)

        self.raise_for_status(response)

        json = response.json()
        return (json["access"], json["refresh"])

    def request(self, method, url, raise_error=True, **kwargs):
        """Generic method to make API requests"""
        # pylint: disable=method-hidden
        logger.info("request: %s - %s - %s", method, url, kwargs)

        # Track if we should set tokens in case of 403/429 response
        set_tokens = kwargs.pop("set_tokens", True)
        full_url = kwargs.pop("full_url", False)

        if not full_url:
            url = f"{self.base_uri}{url}"

        response = self.requests_retry_session(session=self.session).request(
            method, url, timeout=self.timeout, **kwargs
        )
        logger.debug("response: %s - %s", response.status_code, response.content)

        if response.status_code in [403, 429] and set_tokens:
            self._set_tokens()  # Refresh tokens
            kwargs["set_tokens"] = False  # Prevent infinite loop
            return self.request(
                method, url, full_url=True, **kwargs
            )  # Retry the request

        if raise_error:
            self.raise_for_status(response)

        return response

    def __getattr__(self, attr):
        """Generate methods for each HTTP request type (GET, POST, etc.)"""
        methods = ["get", "post", "put", "delete", "patch", "head", "options"]
        if attr in methods:
            return partial(self.request, attr)
        raise AttributeError(
            f"'{self.__class__.__name__}' object has no attribute '{attr}'"
        )

    def requests_retry_session(
        self,
        retries=3,
        backoff_factor=0.3,
        status_forcelist=(500, 502, 504),
        session=None,
    ):
        """Automatic retries for HTTP requests"""

        session = session or requests.Session()
        retry = Retry(
            total=retries,
            read=retries,
            connect=retries,
            backoff_factor=backoff_factor,
            status_forcelist=status_forcelist,
        )
        adapter = HTTPAdapter(max_retries=retry)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def raise_for_status(self, response):
        """Raise for status with a custom error class"""
        try:
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            if exc.response.status_code == 404:
                raise DoesNotExistError(response=exc.response) from exc
            if exc.response.status_code == 401:
                raise CredentialsFailedError(response=exc.response) from exc
            raise APIError(response=exc.response) from exc

    @property
    def user_id(self):
        """Returns the user ID of the user"""
        if self._user_id is None:
            user_data = self._fetch_me()
            self._user_id = user_data["id"]
        return self._user_id
