""" python-squarelet handles authentication and requests to MuckRock services """

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
        elif self.access_token:
            pass  # Already have access token, do nothing
        else:
            raise ValueError("No tokens found")

        if self.access_token:
            self.session.headers.update(
                {"Authorization": f"Bearer {self.access_token}"}
            )

    def _get_tokens(self, username, password):
        """Get an access and refresh token in exchange for the username and password"""
        response = self.requests_retry_session().post(
            f"{self.auth_uri}token/",
            json={"username": username, "password": password},
            timeout=self.timeout,
        )

        if response.status_code == "401":
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
            # refresh token is expired
            return self._get_tokens(self.username, self.password)

        self.raise_for_status(response)

        json = response.json()
        return (json["access"], json["refresh"])

    def request(self, method, url, raise_error=True, **kwargs):
        """Generic method to make API requests"""
        # pylint: disable=method-hidden
        logger.info("request: %s - %s - %s", method, url, kwargs)

        # Add custom headers or other kwargs using the set_request_kwargs method
        custom_kwargs = self.set_request_kwargs(**kwargs)

        # Merge custom kwargs (headers, etc.) with the default kwargs
        kwargs.update(custom_kwargs)

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

    def set_request_kwargs(self, **kwargs):
        """Allow clients to customize request kwargs (e.g., adding headers or versioning)"""
        custom_kwargs = {
            "params": kwargs.get("params", {}),
            "headers": kwargs.get("headers", {}),
        }

        # If the base_uri matches a specific service, add the versioning parameter
        if self.base_uri == "https://api.www.documentcloud.org/api/":
            custom_kwargs["params"]["version"] = "2.0"

        # Allow users to add custom params or headers by passing additional kwargs
        # Merge user-provided arguments with the defaults in custom_kwargs
        if "params" in kwargs:
            custom_kwargs["params"].update(
                kwargs["params"]
            )  # Merge user-specified params

        if "headers" in kwargs:
            custom_kwargs["headers"].update(
                kwargs["headers"]
            )  # Add user-specified headers

        return custom_kwargs

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
