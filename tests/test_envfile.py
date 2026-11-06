from __future__ import annotations

from pathlib import Path

import pytest

from labctl.envfile import (
    PLACEHOLDER_PREFIX,
    EnvError,
    Secrets,
    append_node_vars,
    compile_env,
    parse_env,
    render_env,
)

SHARED = {"TZ": "America/New_York", "OPENAI_API_KEY": "_SECRET_", "PUID": "1000"}
CIRDAN = {
    "DEPLOY_DIR": "/home/paul/docker",
    "APPDATA_DIR": "$DEPLOY_DIR/app_data",
    "LOGS_DIR": "${DEPLOY_DIR}/logs",
}


def test_parse_env() -> None:
    text = "# comment\nA=1\nB = \"two words\"\n\nC='$literal'\nnot a var\n"
    assert parse_env(text) == {"A": "1", "B": "two words", "C": "$literal"}


def test_merge_expand_and_secrets() -> None:
    secrets = Secrets(global_={"OPENAI_API_KEY": "sk-test"})
    env = compile_env("cirdan", [SHARED], CIRDAN, secrets, ["TZ", "APPDATA_DIR"])
    assert env.placeholders == ()
    assert env.values["APPDATA_DIR"] == "/home/paul/docker/app_data"
    assert env.values["LOGS_DIR"] == "/home/paul/docker/logs"
    assert env.values["OPENAI_API_KEY"] == "sk-test"
    assert env.secret_keys == ("OPENAI_API_KEY",) and env.secret_values == {"sk-test"}


def test_node_secret_overrides_global() -> None:
    secrets = Secrets(global_={"OPENAI_API_KEY": "g"}, nodes={"cirdan": {"OPENAI_API_KEY": "n"}})
    env = compile_env("cirdan", [SHARED], CIRDAN, secrets, [])
    assert env.values["OPENAI_API_KEY"] == "n"


def test_missing_without_prompt_gets_placeholders() -> None:
    env = compile_env("cirdan", [SHARED], CIRDAN, Secrets(), ["DATA_DIR", "TZ"])
    assert env.placeholders == ("OPENAI_API_KEY", "DATA_DIR")
    assert env.values["DATA_DIR"] == PLACEHOLDER_PREFIX + "DATA_DIR"


def test_prompted_values_are_returned_for_saving() -> None:
    answers = {"DATA_DIR": "/mnt/data", "OPENAI_API_KEY": "sk-new"}
    asked: list[tuple[str, bool]] = []

    def prompt(node: str, name: str, secret: bool, suggestion: str) -> str | None:
        asked.append((name, secret))
        return answers.get(name)

    env = compile_env("cirdan", [SHARED], CIRDAN, Secrets(), ["DATA_DIR", "TEMP_DIR"], prompt)
    assert asked == [("OPENAI_API_KEY", True), ("DATA_DIR", False), ("TEMP_DIR", False)]
    assert env.new_node_vars == {"DATA_DIR": "/mnt/data"}
    assert env.new_secrets == {"OPENAI_API_KEY": ("sk-new", True)}  # came from _shared: global
    assert env.placeholders == ("TEMP_DIR",)


def test_prompt_is_offered_a_suggestion_from_another_variable() -> None:
    offered: dict[str, str] = {}

    def prompt(node: str, name: str, secret: bool, suggestion: str) -> str | None:
        offered[name] = suggestion
        return suggestion or None

    bilbo = {"APPDATA_DIR": "$HOME_ROOT/docker", "HOME_ROOT": "/home/paul"}
    env = compile_env(
        "bilbo",
        [],
        bilbo,
        Secrets(),
        ["DEPLOY_DIR", "DATA_DIR"],
        prompt,
        {"DEPLOY_DIR": "APPDATA_DIR", "DATA_DIR": "NOT_SET"},
    )
    assert offered == {"DATA_DIR": "", "DEPLOY_DIR": "/home/paul/docker"}
    assert env.new_node_vars == {"DEPLOY_DIR": "/home/paul/docker"}
    assert env.placeholders == ("DATA_DIR",)


def test_variable_referenced_only_by_another_variable_is_missing() -> None:
    env = compile_env("bilbo", [], {"APPDATA_DIR": "$DEPLOY_DIR/x"}, Secrets(), [])
    assert env.placeholders == ("DEPLOY_DIR",)


def test_circular_reference() -> None:
    with pytest.raises(EnvError, match="Circular"):
        compile_env("n", [], {"A": "$B", "B": "$A"}, Secrets(), [])


def test_render_env_is_literal() -> None:
    text = render_env({"PASS": "pa$$word", "QUOTE": "it's", "PLAIN": "x"}, "cirdan")
    assert "PASS='pa$$word'" in text
    assert 'QUOTE="it\'s"' in text
    assert "PLAIN='x'" in text
    with pytest.raises(EnvError):
        render_env({"BAD": "it's $5"}, "cirdan")


def test_append_node_vars(tmp_path: Path) -> None:
    path = tmp_path / "node.env"
    path.write_text("SERVER_IP=10.0.0.1\n\n\n")
    append_node_vars(path, {"DATA_DIR": "/mnt/data", "NOTE": "two words"})
    assert path.read_text().splitlines() == [
        "SERVER_IP=10.0.0.1",
        "",
        "# Added by labctl (referenced by compose files but missing)",
        "DATA_DIR=/mnt/data",
        'NOTE="two words"',
    ]
    assert parse_env(path.read_text())["NOTE"] == "two words"


def test_secrets_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "secrets.toml"
    Secrets(global_={"A": 'q"uote'}, nodes={"cirdan": {"B": "b"}}).save(path)
    loaded = Secrets.load(path)
    assert loaded.get("cirdan", "A") == 'q"uote'
    assert loaded.get("cirdan", "B") == "b"
    assert loaded.get("frodo", "B") is None
