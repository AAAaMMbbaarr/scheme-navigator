# UI smoke test (optional, not part of pytest)
Drives the real `frontend/app.js` in jsdom against a running server. Speech APIs are stubbed, so this checks our
logic (language -> recognition locale, voice selection, stale-profile flow, error messages), NOT real audio.

    npm install jsdom
    python tests/ui_smoke/run_mock_claude_server.py      # port 8766, Claude replaced by a mock
    node tests/ui_smoke/drive_ui.js http://127.0.0.1:8766 fake
    # and against a normal server started WITHOUT ANTHROPIC_API_KEY:
    node tests/ui_smoke/drive_ui.js http://127.0.0.1:8000 nokey
