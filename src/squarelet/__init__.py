# Import core fuctionality
from .squarelet import SquareletClient

# Import exceptions to handle API errors
from squarelet.exceptions import APIError, CredentialsFailedError, DoesNotExistError

# Constants
from .squarelet import BULK_LIMIT, TIMEOUT, RATE_LIMIT, RATE_PERIOD, DEFAULT_AUTH_URI
