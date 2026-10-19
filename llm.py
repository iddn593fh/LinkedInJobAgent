"""OpenAI-compatible chat client that stays inside a free tier's limits.

Limits come from llm.yaml (copy your account's numbers from the provider console) and usage
already spent today comes from the DB, so several runs in a day share one budget. Each limit
is scaled by `safety` so token estimates that run a little low never tip us over.

  per request : estimated input + max_tokens <= min(tpm, context) * safety
  per minute  : requests <= rpm, tokens <= tpm (rolling 60 s window; we sleep to stay under)
  per day     : requests <= rpd, tokens <= tpd (UTC day; when spent, raise LLMStop)

A 429 with a short retry-after is waited out and retried; a long one means the daily quota
is gone, so we stop and leave the remaining jobs for the next run.
"""
import json
import os
import re
import time
from collections import deque

import requests
import yaml

from logutil import log

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm.yaml")
MAX_RETRY_WAIT = 90          # seconds; a longer retry-after means the daily quota is spent


class LLMStop(Exception):
    """No more LLM calls this run (daily budget, quota, or repeated errors)."""


def load_config(path=None):
    with open(path or os.environ.get("JOBAGENT_LLM_CONFIG", CONFIG_PATH)) as f:
        cfg = yaml.safe_load(f) or {}
    cfg["base_url"] = os.environ.get("LLM_BASE_URL", cfg.get("base_url"))
    cfg["model"] = os.environ.get("LLM_MODEL", cfg.get("model"))
    return cfg


def estimate_tokens(text):
    """Conservative: real tokenizers average ~4 chars/token on English, we assume 3."""
    return len(text or "") // 3 + 1


def pack(items, item_tokens, overhead, out_per_item, max_tokens, max_items):
    """Greedy batches: each batch's overhead + inputs + output allowance fits max_tokens.
    An item too big for an empty batch still gets a batch of its own (caller trims inputs)."""
    batches, cur, used = [], [], overhead
    for it in items:
        cost = item_tokens(it) + out_per_item
        if cur and (used + cost > max_tokens or len(cur) >= max_items):
            batches.append(cur)
            cur, used = [], overhead
        cur.append(it)
        used += cost
    if cur:
        batches.append(cur)
    return batches


def parse_json(text):
    """JSON from a model reply, tolerating ```json fences or prose around one object."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise


class LLMClient:
    def __init__(self, cfg, api_key, used_today=(0, 0), record_usage=None, session=None,
                 sleep=time.sleep, clock=time.monotonic, timeout=120):
        lim = cfg["limits"]
        k = float(cfg.get("safety", 0.8))
        self.cfg = cfg
        self.api_key = api_key
        self.rpm, self.rpd = int(lim["rpm"] * k), int(lim["rpd"] * k)
        self.tpm, self.tpd = int(lim["tpm"] * k), int(lim["tpd"] * k)
        self.request_max = int(min(lim["tpm"], lim.get("context", lim["tpm"])) * k)
        self.req_today, self.tok_today = used_today
        self.record_usage = record_usage or (lambda r, t: None)
        self.session = session or requests.Session()
        self.sleep, self.clock, self.timeout = sleep, clock, timeout
        self.window = deque()              # (time, tokens) of requests in the last 60 s
        self.requests = self.tokens = 0    # this run
        self.stopped = None

    # -- budget ----------------------------------------------------------------
    def tokens_left_today(self):
        return max(0, self.tpd - self.tok_today)

    def requests_left_today(self):
        return max(0, self.rpd - self.req_today)

    def _pace(self, est):
        while True:
            t = self.clock()
            while self.window and t - self.window[0][0] >= 60:
                self.window.popleft()
            used = sum(n for _, n in self.window)
            if len(self.window) < self.rpm and used + est <= self.tpm:
                return
            self.sleep(max(0.5, 60 - (t - self.window[0][0]) + 0.5))

    # -- call ------------------------------------------------------------------
    def chat(self, system, user, max_tokens):
        """One completion, JSON mode. Returns parsed JSON. Raises LLMStop when the day's budget
        cannot cover this request or the provider says the quota is spent."""
        if self.stopped:
            raise LLMStop(self.stopped)
        est = estimate_tokens(system) + estimate_tokens(user) + max_tokens
        if self.requests_left_today() < 1 or est > self.tokens_left_today():
            self.stopped = "daily LLM budget used up"
            log(f"llm: {self.stopped} ({self.req_today} requests, {self.tok_today:,} tokens today)")
            raise LLMStop(self.stopped)
        body = {"model": self.cfg["model"], "temperature": 0, "max_tokens": max_tokens,
                "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        body.update(self.cfg.get("extra_body") or {})
        url = self.cfg["base_url"].rstrip("/") + "/chat/completions"
        for attempt in range(4):
            self._pace(est)
            try:
                r = self.session.post(url, json=body, timeout=self.timeout,
                                      headers={"Authorization": f"Bearer {self.api_key}"})
            except requests.RequestException as e:
                if attempt == 3:
                    self.stopped = f"LLM request failed: {e}"
                    raise LLMStop(self.stopped)
                self.sleep(10)
                continue
            if r.status_code == 429:
                wait = _retry_after(r)
                if wait is None or wait > MAX_RETRY_WAIT:
                    self.stopped = "LLM provider quota reached (429)"
                    log(f"llm: 429 with retry-after {wait}, stopping")
                    raise LLMStop(self.stopped)
                log(f"llm: 429, retrying in {wait + 1:.0f}s")
                self.sleep(wait + 1)
                continue
            if r.status_code >= 500 and attempt < 3:
                self.sleep(15)
                continue
            if r.status_code != 200:
                self.stopped = f"LLM HTTP {r.status_code}: {r.text[:200]}"
                log(f"llm: ERROR {self.stopped}")
                raise LLMStop(self.stopped)
            data = r.json()
            used = (data.get("usage") or {}).get("total_tokens") or est
            self.window.append((self.clock(), used))
            self.requests += 1
            self.tokens += used
            self.req_today += 1
            self.tok_today += used
            self.record_usage(1, used)
            log(f"llm #{self.requests}: {used:,} tokens (today {self.req_today} requests, "
                f"{self.tok_today:,}/{self.tpd:,} tokens)")
            remaining = r.headers.get("x-ratelimit-remaining-requests")
            if remaining is not None and remaining.isdigit() and int(remaining) == 0:
                self.req_today = self.rpd          # provider says the day is spent
            return parse_json(data["choices"][0]["message"]["content"])
        self.stopped = "LLM rate limited repeatedly"
        raise LLMStop(self.stopped)


def _retry_after(r):
    v = r.headers.get("retry-after")
    if v is None:
        return 30.0
    try:
        return float(v)
    except ValueError:
        return None
