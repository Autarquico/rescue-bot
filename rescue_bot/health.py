"""Comprobación de salud HTTP declarada en la ficha de cada app.

Solo http(s) a la URL exacta de la ficha; las redirecciones no se siguen, se
informan como estado.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from rescue_bot.config import App


@dataclass(frozen=True)
class Health:
    ok: bool
    status: int | None
    detail: str


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def check(app: App, timeout: float = 10) -> Health:
    if not app.health_url.startswith(("https://", "http://")):
        return Health(False, None, "URL de salud no http(s)")
    req = urllib.request.Request(  # noqa: S310 (esquema comprobado arriba)
        app.health_url, headers={"User-Agent": "rescue-bot"}
    )
    try:
        with _opener.open(req, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return Health(False, None, f"sin respuesta: {e}")
    return Health(status == app.health_expect, status, f"HTTP {status}")


def wait_healthy(app: App, attempts: int = 6, delay: float = 10) -> Health:
    result = check(app)
    for _ in range(attempts - 1):
        if result.ok:
            break
        time.sleep(delay)
        result = check(app)
    return result
