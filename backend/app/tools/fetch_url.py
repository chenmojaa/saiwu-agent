"""URL fetcher with SSRF guard."""
from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import trafilatura

_log = logging.getLogger(__name__)


# Hosts that are NEVER allowed regardless of allow_private: link-local (cloud
# metadata), multicast, reserved, and the well-known AWS / GCP metadata IPs.
_ALWAYS_BLOCKED_NAMES = {"localhost", "metadata.google.internal"}


def _ip_to_block(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address", allow_private: bool) -> bool:
    # Always blocked categories — these are SSRF amplifiers regardless of policy.
    if ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    if ip.is_loopback or ip.is_private:
        return not allow_private
    return False


def _resolve_and_check(host: str, allow_private: bool) -> bool:
    """Return True if the host is blocked under the given policy."""
    if not host:
        return True
    if host.lower() in _ALWAYS_BLOCKED_NAMES:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        # Refuse to connect when DNS fails open — fail closed.
        return True
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if _ip_to_block(ip, allow_private):
            return True
    return False


def _check_url(url: str) -> None:
    """Backward-compatible internal alias (allow_private=False)."""
    check_url(url, allow_private=False)


def check_url(url: str, allow_private: bool = False) -> None:
    """Raise ValueError if the URL targets a blocked host.

    ``allow_private=False`` (default) rejects loopback + RFC1918 in addition
    to the always-blocked link-local / multicast / reserved ranges. Set
    ``allow_private=True`` only for code paths the operator has explicitly
    opted in (e.g. local Ollama at 127.0.0.1). Link-local (169.254/16) and
    cloud-metadata IPs are ALWAYS rejected — this guard cannot be turned off
    from the request body, only by editing this function.
    """
    scheme = ""
    host = ""
    try:
        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        host = parsed.hostname or ""
    except Exception:
        host = ""
    if scheme not in ("http", "https"):
        raise ValueError(f"URL scheme must be http or https: {scheme or '<empty>'}")
    if _resolve_and_check(host, allow_private):
        raise ValueError(f"URL host blocked by SSRF policy: {host or '<empty>'}")


def fetch_url(url: str) -> dict:
  """Fetch URL and extract main text. Returns {title, content, word_count}."""
  _check_url(url)
  downloaded = trafilatura.fetch_url(url)
  if not downloaded:
    raise ValueError(f"无法抓取 URL: {url}")

  text = trafilatura.extract(
    downloaded,
    include_comments=False,
    include_tables=False,
    no_fallback=False,
  )
  if not text:
    raise ValueError(f"未提取到正文: {url}")

  meta = trafilatura.extract_metadata(downloaded)
  title = (meta.title if meta and meta.title else url)[:200]

  return {
    "title": title,
    "content": text,
    "word_count": len(text),
  }
