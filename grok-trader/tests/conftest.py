"""Shared fakes: an HTTP session that answers from a script and never touches the network."""
import pytest

import feed


class FakeResponse:
    def __init__(self, status=200, json_data=None, text="", headers=None):
        self.status_code, self._json, self.text, self.headers = status, json_data, text, headers or {}

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


class FakeSession:
    """Routes by (method, url substring). Each route is a list of responses consumed in order."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def _answer(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for key, responses in self.routes.items():
            if key[0] == method and key[1] in url:
                return responses.pop(0) if len(responses) > 1 else responses[0]
        raise AssertionError(f"unexpected {method} {url}")

    def get(self, url, **kw):
        return self._answer("GET", url, **kw)

    def post(self, url, **kw):
        return self._answer("POST", url, **kw)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(feed, "backoff_sleep", lambda *a, **k: None)
    monkeypatch.setattr(feed.time, "sleep", lambda *a, **k: None)
