"""Polite client for LinkedIn's logged-out (guest) endpoints, shared by search and enrichment.

No login. Each run starts like a browser: it loads the public jobs search page once to get a
LinkedIn visitor identity (bcookie / bscookie / lidc / JSESSIONID / __cf_bm), then sends every
API request in that session over a Chrome TLS / HTTP2-3 fingerprint (curl_cffi).

Why, measured 2026-10-19 from this IP (same IP as a browser that was never throttled):
  python-requests, no cookies .......... 429 after 2-3 requests, even at 60 s spacing
  Chrome fingerprint, no cookies ....... 4/10 ok at 3 s spacing
  Chrome fingerprint + visitor cookies . 15/20 ok at 2 s spacing
LinkedIn rate-limits per visitor; cookieless traffic from an IP shares one small, easily
drained pool, and script-looking TLS is throttled even with cookies.

Rate limiting:
  - randomized wait before every request after the first (callers can ask for a longer one,
    e.g. between searches) and a wall-clock budget for the run
  - 429: retry the same request after a short wait (5-15 s), up to retries_per_request times;
    after `rebootstrap_after` 429s in a row, start a fresh visitor session
  - a request that is still 429 after its retries is skipped (returns None, `throttled` set)
  - `max_failed_in_a_row` skipped requests in a row, or `max_429` 429s in a run: stop the run
  - 403 / 999: hard block, stop the run at once
"""
import random
import time

import requests
from curl_cffi import requests as cffi
from curl_cffi.requests.exceptions import RequestException as CffiError

from logutil import log

IMPERSONATE = "chrome"
BOOTSTRAP_URL = ("https://www.linkedin.com/jobs/search"
                 "?keywords=security&location=United%20States&geoId=103644278")
BLOCK_STATUSES = {403, 999}
RETRY_STATUS = 429


class Stop(Exception):
    """No more LinkedIn requests this run."""


class Blocked(Stop):
    pass


class OutOfTime(Stop):
    pass


def parse_range(s):
    """'10-20' -> (10.0, 20.0); '15' -> (15.0, 15.0)."""
    lo, _, hi = str(s).partition("-")
    lo = float(lo)
    return lo, float(hi) if hi else lo


def chrome_session():
    # The impersonated session sends Chrome's own User-Agent and headers; overriding them
    # would make the fingerprint inconsistent.
    return cffi.Session(impersonate=IMPERSONATE)


class GuestClient:
    def __init__(self, delay=(10.0, 20.0), max_seconds=None, session_factory=chrome_session,
                 sleep=time.sleep, clock=time.monotonic, timeout=20, bootstrap=True,
                 retry_delay=(5.0, 15.0), retries_per_request=3, rebootstrap_after=3,
                 max_failed_in_a_row=3, max_429=60):
        self.delay = delay
        self.session_factory = session_factory
        self.session = session_factory()
        self.sleep = sleep
        self.clock = clock
        self.timeout = timeout
        self.deadline = clock() + max_seconds if max_seconds else None
        self.retry_delay = retry_delay
        self.retries_per_request = retries_per_request
        self.rebootstrap_after = rebootstrap_after
        self.max_failed_in_a_row = max_failed_in_a_row
        self.max_429 = max_429
        self.need_bootstrap = bootstrap
        self.requests = 0              # every HTTP request, bootstrap and retries included
        self.ok = 0
        self.count_429 = 0
        self.skipped = 0               # requests given up on after their retries
        self.throttled = False         # the last get() was skipped for 429s
        self.blocked = None            # HTTP status that stopped us, if any
        self.out_of_time = False
        self._429_streak = 0
        self._failed_streak = 0
        self._last = None

    @property
    def stopped(self):
        return bool(self.blocked or self.out_of_time)

    def stop_reason(self):
        if self.blocked:
            return f"LinkedIn returned {self.blocked}"
        if self.out_of_time:
            return "time budget used up"
        return None

    def summary(self):
        return (f"{self.requests} requests: {self.ok} ok, {self.count_429} x 429, "
                f"{self.skipped} skipped")

    # -- internals -------------------------------------------------------------
    def _wait(self, rng, what):
        wait = 0.0
        if self._last is not None:
            wait = max(0.0, random.uniform(*rng) - (self.clock() - self._last))
        if self.out_of_time or (self.deadline is not None and self.clock() + wait > self.deadline):
            self.out_of_time = True
            log(f"linkedin: time budget used up before {what}")
            raise OutOfTime()
        if wait:
            self.sleep(wait)
        return wait

    def _send(self, url, params, what, wait):
        self.requests += 1
        try:
            r = self.session.get(url, params=params, timeout=self.timeout)
        except (CffiError, requests.RequestException) as e:
            log(f"linkedin #{self.requests} {what} -> network error {type(e).__name__}")
            return None
        finally:
            self._last = self.clock()
        fabric = (r.headers.get("x-li-fabric") or "?").replace("prod-", "")
        log(f"linkedin #{self.requests} {what} -> {r.status_code} [{fabric}] (waited {wait:.0f}s)")
        return r

    def _bootstrap(self):
        """Load the public jobs page once, as a browser would, to get a visitor identity."""
        self.need_bootstrap = False
        wait = self._wait(self.delay, "bootstrap")
        r = self._send(BOOTSTRAP_URL, None, "bootstrap /jobs/search", wait)
        if r is not None and r.status_code == 200:
            names = sorted(c.name for c in self.session.cookies.jar)
            log(f"linkedin: visitor session ready, cookies {names}")
        elif r is not None and r.status_code in BLOCK_STATUSES:
            self._block(r.status_code)
        else:
            log("linkedin: bootstrap failed, continuing without visitor cookies")

    def _rebootstrap(self):
        log(f"linkedin: {self._429_streak} x 429 in a row, starting a fresh visitor session")
        self.session = self.session_factory()
        self._429_streak = 0
        self._bootstrap()

    def _block(self, status):
        self.blocked = status
        log(f"linkedin: BLOCKED with {status}, no more LinkedIn requests this run ({self.summary()})")
        raise Blocked(status)

    # -- public ----------------------------------------------------------------
    def get(self, url, params=None, delay=None):
        """GET text. Returns None on 404 / network errors, or when the request is still 429
        after its retries (then `throttled` is True). Raises Stop when the run must end."""
        if self.blocked:
            raise Blocked(self.blocked)
        self.throttled = False
        if self.need_bootstrap:
            self._bootstrap()
        what = (f"search {params.get('keywords')!r} start={params.get('start')}" if params
                else f"posting {url.rstrip('/').rsplit('/', 1)[-1]}")
        wait = self._wait(delay or self.delay, what)
        for attempt in range(self.retries_per_request + 1):
            if attempt:
                wait = self._wait(self.retry_delay, what)
            r = self._send(url, params, what, wait)
            if r is None:
                return None
            if r.status_code in BLOCK_STATUSES:
                self._block(r.status_code)
            if r.status_code != RETRY_STATUS:
                self._429_streak = 0
                self._failed_streak = 0
                if r.status_code != 200:
                    return None
                self.ok += 1
                return r.text
            self.count_429 += 1
            self._429_streak += 1
            if self.count_429 >= self.max_429:
                log(f"linkedin: {self.count_429} x 429 this run, giving up")
                self._block(RETRY_STATUS)
            if self._429_streak >= self.rebootstrap_after and attempt < self.retries_per_request:
                self._rebootstrap()
        self.skipped += 1
        self._failed_streak += 1
        self.throttled = True
        log(f"linkedin: skipping {what} after {self.retries_per_request + 1} attempts "
            f"({self._failed_streak} skipped in a row)")
        if self._failed_streak >= self.max_failed_in_a_row:
            log(f"linkedin: {self._failed_streak} requests in a row failed on 429, stopping")
            self._block(RETRY_STATUS)
        return None
