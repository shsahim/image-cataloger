import csv
import shutil

from image_cataloger.cli import main


def test_cli_dedupe_only_needs_no_key(tmp_path, make_image, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("image_cataloger.cli.get_api_key", lambda: None)
    folder = tmp_path / "pics"
    folder.mkdir()
    a = make_image("a.png", size=(10, 10))
    shutil.copy(a, folder / "one.png")
    shutil.copy(a, folder / "two.png")
    out = tmp_path / "out" / "cat"
    rc = main([str(folder), "--dedupe-only", "--csv", f"{out}.csv", "--json", f"{out}.json",
               "--db", f"{out}.sqlite"])
    assert rc == 0
    rows = {r["path"]: r for r in csv.DictReader(open(f"{out}.csv"))}
    assert rows[str(folder / "two.png")]["duplicate_of"] == str(folder / "one.png")
    assert "Duplicates:" in capsys.readouterr().out


def test_cli_api_mode_without_key_exits_2(tmp_path, monkeypatch):
    monkeypatch.setattr("image_cataloger.cli.get_api_key", lambda: None)
    assert main([str(tmp_path)]) == 2
