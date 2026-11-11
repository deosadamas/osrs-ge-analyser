import json
from pathlib import Path

from ge_analyser import cli


def test_a_file_the_plugin_is_looking_at_is_written_a_moment_later(tmp_path, monkeypatch):
    target = tmp_path / "orders.json"
    target.write_text('{"seq": 1}', encoding="utf-8")
    swap = Path.replace
    refused = []

    def replace(self, dest):
        if len(refused) < 3:  # Windows: no swap while another program has the target open
            refused.append(dest)
            raise PermissionError(13, "Access is denied")
        return swap(self, dest)

    monkeypatch.setattr(Path, "replace", replace)
    cli._write_json(target, {"seq": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"seq": 2}
    assert len(refused) == 3
