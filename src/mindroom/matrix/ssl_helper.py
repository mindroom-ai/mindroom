"""SSL helper for development environments with self-signed certificates."""

import os
import ssl
import warnings


def get_ssl_context():
    """Get SSL context based on environment settings.

    For development with self-signed certificates, set:
    MATRIX_SSL_VERIFY=false

    WARNING: Only use this in development environments!
    """
    if os.getenv("MATRIX_SSL_VERIFY", "true").lower() == "false":
        warnings.warn(
            "SSL verification is disabled. This is insecure and should only be used in development!", stacklevel=2
        )
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        return ssl_context
    return None  # Use default SSL verification
