import json

from chat_report.adapters.cursor.hooks import (
    BARE_MARKER_MIN_LEN,
    FALLBACK_DENIAL_MARKERS,
    detect_denial,
    extract_hook_script_token,
    load_denial_markers,
    strip_jsonc_comments,
)


def test_strip_jsonc_comments_preserves_string_content():
    raw = """{
      // line comment
      "url": "https://example.test//path",
      /* block comment */
      "hooks": {},
    }"""

    assert json.loads(strip_jsonc_comments(raw)) == {
        "url": "https://example.test//path",
        "hooks": {},
    }


def test_extract_hook_script_token_supports_python_and_javascript():
    assert extract_hook_script_token('python ".cursor/hooks/guard.py" --check') == (
        ".cursor/hooks/guard.py"
    )
    assert extract_hook_script_token("node scripts/guard.mjs") == "scripts/guard.mjs"
    assert extract_hook_script_token("echo no-script") is None


def test_load_denial_markers_scans_hook_scripts(tmp_path):
    hooks_dir = tmp_path / ".cursor" / "hooks"
    hooks_dir.mkdir(parents=True)
    script = hooks_dir / "guard.py"
    script.write_text(
        'result = {"permissionDecision": "deny", "reason": "Policy denied this command"}\n',
        encoding="utf-8",
    )
    (tmp_path / ".cursor" / "hooks.json").write_text(
        """{
          // JSONC comments are supported.
          "hooks": {
            "preToolUse": [{"command": "python .cursor/hooks/guard.py"}],
          },
        }""",
        encoding="utf-8",
    )

    markers = load_denial_markers(tmp_path, ["Custom denial marker"])

    assert "Policy denied this command" in markers
    assert "Custom denial marker" in markers
    assert set(FALLBACK_DENIAL_MARKERS).issubset(markers)
    assert all(len(marker) >= BARE_MARKER_MIN_LEN for marker in markers)


def test_detect_denial_matches_markers_and_hook_context():
    assert detect_denial("failure: Custom denial marker", ("Custom denial marker",)) == (
        True,
        "Custom denial marker",
    )
    denied, reason = detect_denial("The preToolUse hook blocked this operation.")
    assert denied is True
    assert reason is not None
