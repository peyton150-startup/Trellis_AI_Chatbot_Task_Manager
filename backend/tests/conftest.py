"""Deterministic test bootstrap.

One job: make an accidental live provider request impossible in the suites that
are supposed to be deterministic, and make it a loud failure rather than a slow
one.

Pydantic AI exposes `models.ALLOW_MODEL_REQUESTS` for exactly this, and its
testing guidance names it alongside `TestModel` and `FunctionModel`. Trellis
already avoids live calls by construction, since deterministic tests inject a
model and never read `NVIDIA_API_KEY`. This is the backstop for the case that
construction misses: a future change that reaches the production model path from
a test would otherwise either hang on a network call or fail with a credential
error that reads like configuration, when the real defect is that a test tried
to talk to NVIDIA at all.

It is deliberately narrower than a blanket global. The `network` marker is the
one that decides what CI collects, per the marker contract in BUILD_SPEC section
11, and the two external suites carry it. Those tests exist to reach a real
service, so the guard is lifted for exactly them and restored afterwards.

This does not replace the stricter per-module guard in
`test_d76_undo_bridge.py`, which makes provider *construction* raise. That one
proves the D-76 control path never reaches the point where a model would exist,
which is a stronger claim than "no request was sent".
"""

import pytest
from pydantic_ai import models


@pytest.fixture(autouse=True)
def _forbid_live_model_requests(request):
    """Block live provider requests unless the test is marked `network`."""
    original = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = request.node.get_closest_marker("network") is not None
    try:
        yield
    finally:
        models.ALLOW_MODEL_REQUESTS = original
