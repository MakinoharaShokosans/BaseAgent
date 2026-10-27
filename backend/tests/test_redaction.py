"""统一脱敏。出处：docs/08_observability/02_trajectory_and_logs.md §1.4。"""

from __future__ import annotations

import pytest

from agent_runtime.observability import REDACTED, Redactor

KEYS = ["key", "token", "secret", "password", "authorization", "cookie"]
VALUES = [r"sk-[A-Za-z0-9]{16,}", r"ghp_[A-Za-z0-9]{20,}", r"AKIA[0-9A-Z]{16}"]


def make(**kw: object) -> Redactor:
    return Redactor(KEYS, VALUES, **kw)  # type: ignore[arg-type]


@pytest.mark.unit
@pytest.mark.parametrize("key", ["api_key", "GitHubToken", "client_secret", "PASSWORD", "Cookie"])
def test_secret_looking_keys_lose_their_values(key: str) -> None:
    out, changed = make().args({key: "hunter2", "path": "a.py"})
    assert out == {key: REDACTED, "path": "a.py"} and changed


@pytest.mark.unit
def test_nested_structures_are_walked_without_touching_the_input() -> None:
    original = {"a": [{"token": "x"}, {"ok": 1}], "b": ("c", {"password": "p"})}
    out = make().value(original)
    assert out == {"a": [{"token": REDACTED}, {"ok": 1}], "b": ["c", {"password": REDACTED}]}
    assert original["a"][0] == {"token": "x"}


@pytest.mark.unit
@pytest.mark.parametrize(
    "secret",
    ["sk-abcdefghijklmnop1234", "ghp_abcdefghijklmnopqrstuv", "AKIAABCDEFGHIJKLMNOP"],
)
def test_known_secret_shapes_are_replaced_in_free_text(secret: str) -> None:
    assert make().text(f"use {secret} now") == f"use {REDACTED} now"


@pytest.mark.unit
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ('curl -H "Authorization: Bearer abc.def" https://x', "Authorization: Bearer " + REDACTED),
        ("tool --password hunter2 run", "--password " + REDACTED),
        ("tool --token=abc123 run", "--token=" + REDACTED),
        ("API_KEY=zzz123 python x.py", "API_KEY=" + REDACTED),
        ("git clone https://bob:pw123@host/repo", "https://bob:" + REDACTED + "@host/repo"),
    ],
)
def test_credentials_inside_commands_are_replaced(command: str, expected: str) -> None:
    out = make().text(command)
    assert expected in out
    for leaked in ("abc.def", "hunter2", "abc123", "zzz123", "pw123"):
        assert leaked not in out


@pytest.mark.unit
def test_ordinary_commands_are_untouched() -> None:
    cmd = "mkdir -p build && ls -la src | grep keyboard.py && pytest -x -q"
    assert make().text(cmd) == cmd


@pytest.mark.unit
def test_long_high_entropy_strings_are_replaced_but_hashes_and_words_survive() -> None:
    blob = "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5bC7dE9"
    assert make().text(f"v={blob}") == f"v={REDACTED}"
    sha = "0123456789abcdef" * 4
    assert make().text(sha) == sha
    assert make().text("a" * 40) == "a" * 40


@pytest.mark.unit
def test_canary_like_secrets_are_replaced_exactly() -> None:
    r = make().with_secrets(["canary-xyz-12345"])
    assert r.text("leak canary-xyz-12345!") == f"leak {REDACTED}!"
    assert make().text("leak canary-xyz-12345!") == "leak canary-xyz-12345!"  # 原对象不变


@pytest.mark.unit
def test_too_short_secrets_are_not_used_as_patterns() -> None:
    assert make(secrets=["ab"]).text("about") == "about"


@pytest.mark.unit
def test_disabled_redactor_is_the_identity() -> None:
    r = make(enabled=False)
    data = {"token": "x", "c": "sk-abcdefghijklmnop1234"}
    assert r.value(data) == data and r.text("sk-abcdefghijklmnop1234") == "sk-abcdefghijklmnop1234"


@pytest.mark.unit
def test_args_reports_whether_anything_changed() -> None:
    assert make().args({"command": "ls"}) == ({"command": "ls"}, False)
    assert make().args({"command": "A_TOKEN=abcdef ls"})[1] is True


@pytest.mark.unit
def test_non_string_scalars_pass_through() -> None:
    assert make().value({"n": 1, "f": 1.5, "b": True, "none": None}) == {
        "n": 1,
        "f": 1.5,
        "b": True,
        "none": None,
    }
