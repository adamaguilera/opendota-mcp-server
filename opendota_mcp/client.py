"""
HTTP client and rate limiter management
"""
import asyncio
import random
import httpx
import logging
from typing import Optional, Dict, Any
from .classes import RateLimiter
from .config import OPENDOTA_BASE_URL, RATE_LIMIT_RPM, OPENDOTA_API_KEY

logger = logging.getLogger("opendota-server")

# --- Retry policy for transient failures ---
# OpenDota's aggregation endpoints (/wl, /peers, /totals) are computed on demand and then cached.
# A first request can therefore time out while the result is still being built — a short retry then
# usually hits the now-warm cache and returns fast. Network blips and 429/5xx are likewise transient.
# Worst case ~ read_timeout * MAX_ATTEMPTS + backoffs (~32s with the values below); keep any caller
# timeout comfortably above that so this layer (not the caller) gets to surface a clear error.
MAX_ATTEMPTS = 3
RETRY_BACKOFF_BASE = 0.75  # seconds; grows ~2x per attempt, plus a little jitter
RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# Global instances
rate_limiter = RateLimiter(requests_per_minute=RATE_LIMIT_RPM)
http_client: Optional[httpx.AsyncClient] = None


async def get_http_client() -> httpx.AsyncClient:
    """Get or create the async HTTP client."""
    global http_client
    if http_client is None:
        headers = {}

        # Add API key to Authorization header if available
        if OPENDOTA_API_KEY:
            headers["Authorization"] = f"Bearer {OPENDOTA_API_KEY}"
            logger.info(f"Using API key: {OPENDOTA_API_KEY[:3]}...")
        else:
            logger.info("HTTP client initialized (anonymous access)")

        http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=10.0,
                read=10.0,  # fail a hung read fast so a retry can hit OpenDota's warmed cache
                write=10.0,
                pool=10.0
            ),
            limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            headers=headers
        )
    return http_client


async def cleanup_http_client():
    """Close the HTTP client on shutdown."""
    global http_client
    if http_client is not None:
        await http_client.aclose()
        http_client = None
        logger.info("HTTP client closed")


async def fetch_api(endpoint: str, params: Optional[Dict[str, Any]] = None) -> dict:
    """
    Fetch data from OpenDota API with rate limiting.

    API key is automatically included via Authorization header if configured.

    Args:
        endpoint: API endpoint path
        params: Query parameters

    Returns:
        JSON response from API
    """
    client = await get_http_client()

    if params is None:
        params = {}

    url = f"{OPENDOTA_BASE_URL}{endpoint}"
    logger.debug(f"Fetching data from {url}, with params: {params}")

    last_exc: Optional[Exception] = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        await rate_limiter.acquire()  # respect the rate limit on every attempt, not just the first
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status not in RETRYABLE_STATUS:
                # 4xx (other than 429) is a deterministic client error — don't waste retries on it.
                logger.error(f"HTTP {status} error from {url}: {e.response.text[:200]}")
                raise
            last_exc, reason = e, f"HTTP {status}"
        except httpx.TransportError as e:
            # Covers timeouts (ReadTimeout/ConnectTimeout) and network errors — all worth retrying.
            last_exc, reason = e, type(e).__name__

        if attempt < MAX_ATTEMPTS:
            delay = RETRY_BACKOFF_BASE * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
            logger.warning(
                f"{reason} from {url} (attempt {attempt}/{MAX_ATTEMPTS}); retrying in {delay:.1f}s"
            )
            await asyncio.sleep(delay)

    # Exhausted retries. Raise a clear, non-empty message — some transient errors (e.g.
    # httpx.ReadTimeout) stringify to "", which would otherwise surface as an empty tool error.
    msg = (
        f"OpenDota request to {endpoint} failed after {MAX_ATTEMPTS} attempts "
        f"({type(last_exc).__name__})"
    )
    logger.error(msg)
    raise RuntimeError(msg) from last_exc