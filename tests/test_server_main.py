import json
import sys

import pytest

from multilayer_optical_mcp import server
from multilayer_optical_network.state_file import dump_state, topology_fingerprint
from multilayer_optical_network.testing import TOPOLOGY, _built


def test_server_main_rejects_state_without_topology(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["multilayer-optical-mcp", "--state", "s.json"])
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code != 0
    assert "--topology" in capsys.readouterr().err


def test_server_main_reports_a_fingerprint_mismatch_without_a_traceback(
        monkeypatch, tmp_path, capsys):
    # The realistic real-world failure: topology got rebuilt, state file did
    # not. main() must fail with a structured message, not a raw traceback.
    other = tmp_path / "other.json"
    changed = json.loads(json.dumps(TOPOLOGY))
    changed["graph"]["edges"][0]["length_km"] = 999.0
    other.write_text(json.dumps(changed), encoding="utf-8")

    state = tmp_path / "state.json"
    doc = dump_state(_built(), fingerprint=topology_fingerprint(TOPOLOGY), meta={})
    state.write_text(json.dumps(doc), encoding="utf-8")

    monkeypatch.setattr(sys, "argv", [
        "multilayer-optical-mcp", "--topology", str(other), "--state", str(state)])
    with pytest.raises(SystemExit) as exc:
        server.main()
    assert exc.value.code != 0
    err = capsys.readouterr().err
    assert "sha256:" in err
    assert str(other) in err and str(state) in err
    assert "Traceback" not in err
