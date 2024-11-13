"""
Custom exceptions for python-squarelet
"""


class SquareletError(Exception):
    """Base class for errors for python-squarelet"""

    def __init__(self, *args, **kwargs):
        self.response = kwargs.pop("response", None)
        if self.response is not None:
            self.error = self.response.text
            self.status_code = self.response.status_code
            if not args:
                args = [f"{self.status_code} - {self.error}"]
        else:
            self.error = None
            self.status_code = None
        super().__init__(*args, **kwargs)


class DuplicateObjectError(SquareletError):
    """Raised when an object is added to a unique list more than once"""


class CredentialsFailedError(SquareletError):
    """Raised if unable to obtain an access token due to bad login credentials"""


class APIError(SquareletError):
    """Any other error calling an API"""


class DoesNotExistError(APIError):
    """Raised when the user asks the API for something it cannot find"""


class MultipleObjectsReturnedError(APIError):
    """Raised when the API returns multiple objects when it expected one"""
