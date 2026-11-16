from pathlib import Path

import yaml

from tests.conftest import requires_agent_modules


def _write_skill(root: Path, name: str):
    skill_dir = root / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n\n# {name}\n",
        encoding="utf-8",
    )


def _write_config(home: Path, disabled):
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.safe_dump({"skills": {"disabled": list(disabled)}}, sort_keys=False),
        encoding="utf-8",
    )


def _load_config(home: Path):
    return yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8")) or {}


@requires_agent_modules
def test_skills_list_reads_disabled_state_from_active_profile(monkeypatch, tmp_path):
    """#3066: the skill directory and disabled toggle state must use the same profile."""
    from api import profiles, routes

    default_home = tmp_path / "default"
    active_home = tmp_path / "profiles" / "auditor"
    for name in ("alpha", "beta"):
        _write_skill(active_home, name)
    _write_config(default_home, ["alpha"])
    _write_config(active_home, ["beta"])

    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: active_home)

    listed = routes._skills_list_from_dir(active_home / "skills")["skills"]
    by_name = {skill["name"]: skill for skill in listed}

    assert by_name["alpha"]["disabled"] is False
    assert by_name["beta"]["disabled"] is True


@requires_agent_modules
def test_skill_toggle_writes_active_profile_config_not_default(monkeypatch, tmp_path):
    """#3066: WebUI toggle writes the active profile config, not default HERMES_HOME."""
    from api import profiles, routes

    default_home = tmp_path / "default"
    active_home = tmp_path / "profiles" / "trader"
    _write_skill(active_home, "gamma")
    _write_config(default_home, [])
    _write_config(active_home, ["gamma"])

    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: active_home)
    monkeypatch.setattr(routes, "reload_config", lambda: None)
    monkeypatch.setattr(routes, "j", lambda _handler, payload: payload)
    monkeypatch.setattr(routes, "bad", lambda _handler, message, status=400: {"error": message, "status": status})

    enabled_response = routes._handle_skill_toggle(None, {"name": "gamma", "enabled": True})
    assert enabled_response == {"ok": True, "name": "gamma", "enabled": True}
    assert _load_config(active_home)["skills"]["disabled"] == []
    assert _load_config(default_home)["skills"]["disabled"] == []

    disabled_response = routes._handle_skill_toggle(None, {"name": "gamma", "enabled": False})
    assert disabled_response == {"ok": True, "name": "gamma", "enabled": False}
    assert _load_config(active_home)["skills"]["disabled"] == ["gamma"]
    assert _load_config(default_home)["skills"]["disabled"] == []


def _protected_skill_profile(monkeypatch, tmp_path, *, via_env=False):
    from api import profiles, routes

    home = tmp_path / "profiles" / "compliance"
    _write_skill(home, "public")
    protected_root = home / "skills" / "internal"
    secret_dir = protected_root / "secret"
    secret_dir.mkdir(parents=True)
    (secret_dir / "SKILL.md").write_text(
        "---\nname: secret\ndescription: hidden description\n---\n\n# Secret body\n",
        encoding="utf-8",
    )
    (secret_dir / "ref.md").write_text("hidden reference", encoding="utf-8")
    cfg = {"skills": {"disabled": []}}
    if via_env:
        monkeypatch.setenv("HERMES_WEBUI_PROTECTED_SKILL_DIRS", str(protected_root))
    else:
        monkeypatch.delenv("HERMES_WEBUI_PROTECTED_SKILL_DIRS", raising=False)
        cfg["webui"] = {"protected_skill_dirs": [str(protected_root)]}
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(profiles, "get_active_hermes_home", lambda: home)
    monkeypatch.setattr(routes, "reload_config", lambda: None)
    monkeypatch.setattr(routes, "j", lambda _handler, payload, **_kwargs: payload)
    monkeypatch.setattr(
        routes, "bad", lambda _handler, message, status=400: {"error": message, "status": status}
    )
    return routes, home, secret_dir


@requires_agent_modules
def test_protected_skills_are_listed_by_name_only(monkeypatch, tmp_path):
    routes, home, _secret_dir = _protected_skill_profile(monkeypatch, tmp_path)

    by_name = {s["name"]: s for s in routes._skills_list_from_dir(home / "skills")["skills"]}

    assert by_name["secret"] == {
        "name": "secret", "description": "", "category": None, "disabled": False, "protected": True,
    }
    assert by_name["public"]["description"] == "public skill"
    assert "protected" not in by_name["public"]


@requires_agent_modules
def test_protected_skill_dirs_env_var_is_honoured(monkeypatch, tmp_path):
    routes, home, _secret_dir = _protected_skill_profile(monkeypatch, tmp_path, via_env=True)

    by_name = {s["name"]: s for s in routes._skills_list_from_dir(home / "skills")["skills"]}

    assert by_name["secret"].get("protected") is True


@requires_agent_modules
def test_protected_skill_content_and_files_are_refused(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from urllib.parse import urlencode

    routes, _home, _secret_dir = _protected_skill_profile(monkeypatch, tmp_path)
    responses = []
    monkeypatch.setattr(routes, "_guard_request_session_visibility", lambda *_a, **_k: True)
    monkeypatch.setattr(routes, "bad", lambda _h, message, status=400: responses.append((status, message)) or True)
    monkeypatch.setattr(routes, "j", lambda _h, payload, **_k: responses.append((200, payload)) or True)

    for query in ({"name": "secret"}, {"name": "secret", "file": "ref.md"}):
        routes.handle_get(None, SimpleNamespace(path="/api/skills/content", query=urlencode(query)))
    routes.handle_get(None, SimpleNamespace(path="/api/skills/content", query=urlencode({"name": "public"})))

    assert responses[0] == (403, routes._PROTECTED_SKILL_ERROR)
    assert responses[1] == (403, routes._PROTECTED_SKILL_ERROR)
    assert responses[2][0] == 200 and "# public" in responses[2][1]["content"]


@requires_agent_modules
def test_protected_skill_cannot_be_toggled_saved_shadowed_or_deleted(monkeypatch, tmp_path):
    routes, home, secret_dir = _protected_skill_profile(monkeypatch, tmp_path)
    original = (secret_dir / "SKILL.md").read_text(encoding="utf-8")
    refused = {"error": routes._PROTECTED_SKILL_ERROR, "status": 403}

    assert routes._handle_skill_toggle(None, {"name": "secret", "enabled": False}) == refused
    assert routes._handle_skill_save(None, {"name": "secret", "content": "x"}) == refused
    assert routes._handle_skill_save(
        None, {"name": "secret", "category": "internal", "content": "x"}
    ) == refused
    assert routes._handle_skill_delete(None, {"name": "secret"}) == refused

    assert (secret_dir / "SKILL.md").read_text(encoding="utf-8") == original
    assert not (home / "skills" / "secret").exists()
    assert _load_config(home)["skills"]["disabled"] == []


def _skill_view_result(name, skill_dir, body):
    import json

    return json.dumps({
        "success": True, "name": name, "description": "d", "content": body,
        "path": f"{name}/SKILL.md", "skill_dir": str(skill_dir), "linked_files": None,
    })


@requires_agent_modules
def test_public_session_payload_withholds_protected_skill_results(monkeypatch, tmp_path):
    import json

    from api.helpers import redact_session_data

    routes, home, secret_dir = _protected_skill_profile(monkeypatch, tmp_path)

    def _call(tid, skill):
        return {"id": tid, "type": "function",
                "function": {"name": "skill_view", "arguments": json.dumps({"name": skill})}}

    session = {
        "session_id": "s1",
        "messages": [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [_call("t1", "secret"), _call("t2", "public")]},
            {"role": "tool", "tool_call_id": "t1", "content": _skill_view_result("secret", secret_dir, "# Secret body")},
            {"role": "tool", "tool_call_id": "t2", "content": _skill_view_result("public", home / "skills" / "public", "# public")},
            # A tool row whose issuing assistant turn is outside the loaded window.
            {"role": "tool", "tool_call_id": "t9", "content": _skill_view_result("secret", secret_dir, "# Secret body")},
        ],
        "tool_calls": [
            {"name": "skill_view", "tid": "t1", "snippet": '{"success": true, "content": "# Secret bo', "args": {"name": "secret"}},
            {"name": "skill_view", "tid": "t2", "snippet": "# public", "args": {"name": "public"}},
        ],
    }

    public = redact_session_data(session)
    text = json.dumps(public)

    assert "Secret body" not in text and "Secret bo" not in text
    withheld = {"success": True, "name": "secret", "status": "succeeded"}
    assert json.loads(public["messages"][2]["content"]) == withheld
    assert json.loads(public["messages"][4]["content"]) == withheld
    assert json.loads(public["tool_calls"][0]["snippet"]) == withheld
    assert "# public" in public["messages"][3]["content"]
    assert public["tool_calls"][1]["snippet"] == "# public"
    assert "Secret body" in session["messages"][2]["content"]


@requires_agent_modules
def test_live_tool_result_withholds_only_protected_skills(monkeypatch, tmp_path):
    import json

    from api.streaming import _withhold_tool_result

    routes, home, secret_dir = _protected_skill_profile(monkeypatch, tmp_path)
    secret = _skill_view_result("secret", secret_dir, "# Secret body")
    public = _skill_view_result("public", home / "skills" / "public", "# public")
    failure = json.dumps({"success": False, "error": "Skill 'secret' is disabled."})

    assert json.loads(_withhold_tool_result("skill_view", {"name": "secret"}, secret))["status"] == "succeeded"
    assert _withhold_tool_result("skill_view", {"name": "public"}, public) == public
    assert _withhold_tool_result("skill_view", {"name": "secret"}, failure) == failure
    assert _withhold_tool_result("read_file", {"path": "x"}, secret) == secret
