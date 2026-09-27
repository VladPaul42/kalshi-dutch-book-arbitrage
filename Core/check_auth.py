"""Make exactly ONE signed Kalshi request and say clearly whether it worked.

Run this first, and don't debug anything else until it passes. Authentication is
the fiddly part of this API and it is far easier to diagnose on its own.

This script is deliberately STANDALONE: it duplicates the signing logic from
kalshi/client.py rather than importing it, so that when authentication breaks
there is exactly one short self-contained file to stare at.

Three things about this API cause almost all signature failures:

  1. The signed path must INCLUDE the /trade-api/v2 prefix.
  2. Query parameters must be STRIPPED before signing.
  3. The timestamp is part of the signed message, so headers must be rebuilt on
     every attempt rather than reused.

And one thing about this machine: it has a TLS-intercepting root certificate
(antivirus or a proxy) that lives in the Windows certificate store but not in
Python's certifi bundle. Browsers and curl work; Python fails with
CERTIFICATE_VERIFY_FAILED. truststore fixes that by verifying against the OS
store. Never "fix" it by disabling verification.

Nothing here prints the key ID or any key material.
"""

from __future__ import annotations

import base64
import os
import ssl
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import truststore
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from dotenv import load_dotenv

BASE_URLS = {
    "demo": "https://external-api.demo.kalshi.co/trade-api/v2",
    "production": "https://external-api.kalshi.com/trade-api/v2",
}

# The one endpoint this script calls. Read-only.
PATH = "/portfolio/balance"


def fail(message: str) -> None:
    """Print a specific diagnosis and exit non-zero."""
    print()
    print("AUTHENTICATION FAILED")
    print()
    print(message)
    sys.exit(1)


def main() -> None:
    load_dotenv()

    env = (os.getenv("KALSHI_ENV") or "demo").strip().lower()
    if env in ("prod", "production"):
        env = "production"
    if env not in BASE_URLS:
        fail(
            f"KALSHI_ENV in .env is {env!r}, which is not a valid environment.\n"
            f"Set it to 'demo' or 'production'."
        )

    prefix = "KALSHI_DEMO" if env == "demo" else "KALSHI_PROD"
    base_url = (os.getenv(f"{prefix}_BASE_URL") or BASE_URLS[env]).rstrip("/")

    key_id_path = (os.getenv(f"{prefix}_API_KEY_ID_PATH") or "").strip()
    private_key_path = (os.getenv(f"{prefix}_PRIVATE_KEY_PATH") or "").strip()

    print(f"Environment : {env.upper()}")
    print(f"Base URL    : {base_url}")
    print(f"Request     : GET {PATH}")
    print()

    if not key_id_path or not private_key_path:
        fail(
            f"No {env} credentials are configured in .env.\n"
            f"Set {prefix}_API_KEY_ID_PATH and {prefix}_PRIVATE_KEY_PATH to point at\n"
            f"your key files. They should hold PATHS to the files, not the key values."
        )

    # --- read the key ID ---------------------------------------------------
    try:
        api_key_id = Path(key_id_path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        fail(
            f"Could not read the API key ID file.\n"
            f"  path  : {key_id_path}\n"
            f"  error : {exc}\n\n"
            f"Check that {prefix}_API_KEY_ID_PATH in .env points at a file that exists."
        )

    if not api_key_id:
        fail(
            f"The API key ID file is empty.\n"
            f"  path : {key_id_path}\n\n"
            f"It should contain your key ID on a single line."
        )

    if "\n" in api_key_id:
        fail(
            f"The API key ID file has more than one line, so it is probably not just\n"
            f"a key ID.\n  path : {key_id_path}"
        )

    # --- read and parse the private key ------------------------------------
    try:
        pem_bytes = Path(private_key_path).read_bytes()
    except OSError as exc:
        fail(
            f"Could not read the private key file.\n"
            f"  path  : {private_key_path}\n"
            f"  error : {exc}\n\n"
            f"Check that {prefix}_PRIVATE_KEY_PATH in .env points at a file that exists."
        )

    try:
        private_key = serialization.load_pem_private_key(pem_bytes, password=None)
    except Exception as exc:
        fail(
            f"Could not parse the private key as an unencrypted PEM.\n"
            f"  path  : {private_key_path}\n"
            f"  error : {exc}\n\n"
            f"Expected the file Kalshi generated, whose first line is\n"
            f"'-----BEGIN RSA PRIVATE KEY-----' or '-----BEGIN PRIVATE KEY-----'.\n"
            f"If you pasted the key into a new file, a missing trailing newline or\n"
            f"Windows line endings inside the base64 body will both break parsing."
        )

    if not isinstance(private_key, rsa.RSAPrivateKey):
        fail(
            f"The private key parsed, but it is not an RSA key ({type(private_key).__name__}).\n"
            f"Kalshi signatures are RSA-PSS, so an RSA key is required."
        )

    # --- build the signed headers -----------------------------------------
    url = base_url + PATH
    # urlsplit().path already drops the query string; the explicit split is
    # belt-and-braces in case a caller inlines a query into the path.
    signing_path = urlsplit(url).path.split("?")[0]

    if not signing_path.startswith("/trade-api/v2"):
        fail(
            f"The path about to be signed is {signing_path!r}, which does not start with\n"
            f"'/trade-api/v2'. Kalshi signs the full path from the API root, so the\n"
            f"signature will be rejected.\n\n"
            f"This means the base URL in .env is missing the /trade-api/v2 suffix.\n"
            f"  base URL : {base_url}"
        )

    timestamp_ms = str(int(time.time() * 1000))
    message = f"{timestamp_ms}GET{signing_path}".encode("utf-8")

    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            # Salt length equals the digest length. Kalshi rejects PSS.MAX_LENGTH.
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )

    headers = {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode("utf-8"),
        "Accept": "application/json",
    }

    # --- send it ----------------------------------------------------------
    ssl_context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    try:
        response = httpx.get(url, headers=headers, timeout=30.0, verify=ssl_context)
    except httpx.ConnectError as exc:
        detail = str(exc)
        if "CERTIFICATE_VERIFY_FAILED" in detail:
            fail(
                f"TLS certificate verification failed even with truststore enabled.\n"
                f"  error : {detail}\n\n"
                f"This machine intercepts TLS, so Python must verify against the Windows\n"
                f"certificate store. truststore was used here, so if this still fails the\n"
                f"intercepting root certificate may not be installed in the store where\n"
                f"Python looks. Do NOT disable certificate verification to get past this."
            )
        fail(
            f"Could not connect to {base_url}.\n"
            f"  error : {detail}\n\n"
            f"Check the hostname in .env and your network connection. Kalshi's hostnames\n"
            f"have changed over time, so confirm it against the current docs."
        )
    except httpx.HTTPError as exc:
        fail(f"The request failed before a response arrived.\n  error : {exc}")

    if response.status_code in (401, 403):
        fail(
            f"Kalshi rejected the credentials with HTTP {response.status_code}.\n"
            f"  response : {response.text[:300]}\n\n"
            f"The three usual causes, in order of likelihood:\n"
            f"  1. Environment mismatch. KALSHI_ENV is {env!r} and the request went to\n"
            f"     {base_url}. Demo keys ONLY work on demo hosts and production keys\n"
            f"     ONLY on production hosts.\n"
            f"  2. The key ID and the private key are not a matching pair.\n"
            f"  3. Clock skew. The timestamp is signed, so a system clock that is off by\n"
            f"     more than a few seconds invalidates the signature."
        )

    if response.status_code == 404:
        fail(
            f"HTTP 404 for {url}\n\n"
            f"The base URL probably already ends in /trade-api/v2 and the path was added\n"
            f"twice. The request URL should contain /trade-api/v2 exactly once."
        )

    if response.status_code >= 400:
        fail(
            f"HTTP {response.status_code} from GET {PATH}\n"
            f"  response : {response.text[:300]}"
        )

    try:
        payload = response.json()
    except ValueError:
        fail(
            f"The response was HTTP {response.status_code} but the body was not JSON.\n"
            f"  body : {response.text[:200]}"
        )

    print("AUTHENTICATION OK")
    print()

    # balance_dollars is the exact fixed-point value. The legacy integer-cent
    # `balance` field truncates anything below a cent.
    balance_dollars = payload.get("balance_dollars")
    if balance_dollars is not None:
        print(f"Account balance : ${balance_dollars}")
    elif payload.get("balance") is not None:
        # Legacy integer cents, kept only as a fallback.
        print(f"Account balance : {payload['balance']} cents (legacy integer field)")
    else:
        print("Signed request succeeded, but the response had no balance field.")
        print(f"Response keys : {sorted(payload)}")


if __name__ == "__main__":
    main()
