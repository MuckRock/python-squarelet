"""python-squarelet handles authentication and requests to MuckRock services"""

# Standard Library
import base64
import json
import logging
import time
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
TOKEN_EXPIRY_LEEWAY = 30

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
        # Default UA for unauthenticated requests.
        existing_ua = self.session.headers.get("User-Agent", "")
        self.session.headers.update({"User-Agent": f"{existing_ua} Anonymous".strip()})
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

    def _token_expiring(self, leeway=TOKEN_EXPIRY_LEEWAY):
        """
        True if the access token is missing or within `leeway` seconds of expiry.

        Lets us refresh proactively instead of sending a known-expired token,
        which the server treats as anonymous. An anonymous request can then hit
        the anonymous rate quota on DocumentCloud and return a 429 that the
        401/403 auth-recovery branch does not handle. We can't add 429 to that auth
        recovery branch as there are legitimate rate limits that return a 429
        that we don't want to call set_tokens on repeatedly.

        If the token is not a parseable JWT with an exp claim, returns False and
        we fall back to the reactive 401/403 path.
        """
        if not self.access_token:
            return True
        try:
            payload = self.access_token.split(".")[1]
            payload += "=" * (-len(payload) % 4)  # pad base64
            exp = json.loads(base64.urlsafe_b64decode(payload))["exp"]
        except (IndexError, ValueError, KeyError, TypeError):
            return False  # unparseable; let the server decide via 401/403
        return exp < time.time() + leeway

    def _set_tokens(self):
        """Set the refresh and access tokens"""
        if self.refresh_token:
            logger.info("_set_tokens: refreshing via refresh_token")
            self.access_token, self.refresh_token = self._refresh_tokens(
                self.refresh_token
            )
        elif self.username and self.password:
            logger.info("_set_tokens: refreshing via username/password")
            self.access_token, self.refresh_token = self._get_tokens(
                self.username, self.password
            )
        else:
            logger.warning("_set_tokens: NO CREDENTIALS - dropping to anonymous")
            self.access_token = None
            self.refresh_token = None
        if self.access_token:
            self.session.headers.update(
                {"Authorization": f"Bearer {self.access_token}"}
            )
            # Identify authed users to better manage API usage.
            if self.username:
                existing_ua = self.session.headers.get("User-Agent", "")
                new_ua = existing_ua.replace("Anonymous", self.username).strip()
                self.session.headers.update({"User-Agent": new_ua})

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

        data = response.json()
        return (data["access"], data["refresh"])

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

        data = response.json()
        return (data["access"], data["refresh"])

    def request(self, method, url, raise_error=True, **kwargs):
        """Generic method to make API requests"""
        # pylint: disable=method-hidden
        logger.info("request: %s - %s - %s", method, url, kwargs)

        # Track if we should set tokens in case of 401/403 response
        set_tokens = kwargs.pop("set_tokens", True)
        full_url = kwargs.pop("full_url", False)

        # Proactive refresh: don't send a known-expired token and risk being
        # bucketed as anonymous. Only refresh if we have a way to; a client
        # with no credentials stays anonymous and lets the server respond.
        if (
            set_tokens
            and self._token_expiring()
            and (self.refresh_token or (self.username and self.password))
        ):
            logger.info("request: token expiring/expired, refreshing before send")
            self._set_tokens()

        if not full_url:
            url = f"{self.base_uri}{url}"

        response = self.requests_retry_session(session=self.session).request(
            method, url, timeout=self.timeout, **kwargs
        )
        logger.debug("response: %s - %s", response.status_code, response.content)

        if response.status_code in [401, 403] and set_tokens:
            logger.info(
                "request: got %s, calling _set_tokens and retrying",
                response.status_code,
            )
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
            user_data = self.request("get", "users/me/").json()
            user_id = user_data["id"]
            self._user_id = user_id
        return self._user_id
