# python-squarelet
A Python wrapper for Squarelet, allowing for authentication with MuckRock systems like MuckRock Requests and DocumentCloud.

## Features
- Token-based Authentication: Easily authenticate with MuckRock services using username/password or refresh tokens.
- Rate Limiting: Automatically applies rate limiting to prevent exceeding API request limits.
- Automatic Token Refresh: Seamlessly refresh your access tokens when expired without manual intervention.
- Retry Logic: Built-in automatic retries for failed requests, ensuring robust interaction with MuckRock services.
- API Request Handling: Send GET, POST, PUT, DELETE, and other HTTP requests to MuckRock’s API with an easy-to-use interface.
- Customizable Request Headers: Extend or modify request parameters (like headers) through flexible client configuration.
- Error Handling: Provides detailed error handling with custom exceptions for API errors, credentials issues, and missing resources.

## Installation

```bash
$ pip install python-squarelet
```

## Usage
```python
# If you intend on accessing MuckRock requests, the base_uri would be https://www.muckrock.com/api_v1/ or https://www.muckrock.com/api_v2/
base_uri="https://api.www.documentcloud.org/api/"
from squarelet import SquareletClient
# Authenticating with Squarelet using your credentials
client = SquareletClient(base_uri=base_uri, username="your_username", password="your_password")
# Example API call that gets data about your DocumentCloud account.
my_user = client.request("get", "users/me/")
# Print the response data about your DocumentCloud account. 
print(my_user.text) 
```

Generally, it is safer to access your username as local environment variables using [os.environ](https://docs.python.org/3/library/os.html#os.environ) instead of specifying your username and password as strings, especially if publishing this code anywhere. After setting your local environment variables SQ_USERNAME and SQ_PASSWORD with your credentials, you can access them like so. 
```python
username = os.environ["SQ_USERNAME"]
password = os.environ["SQ_PASSWORD"]
```
