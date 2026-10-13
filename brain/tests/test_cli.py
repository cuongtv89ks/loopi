from loopi_brain import cli


def test_init_pki_command_prints_ca_fingerprint(tmp_path, capsys):
    assert cli.main(["init-pki", "--pki-dir", str(tmp_path), "--host", "brain.lan"]) == 0
    assert "CA fingerprint:" in capsys.readouterr().out
    assert cli.main(["init-pki", "--pki-dir", str(tmp_path), "--host", "brain.lan"]) == 1


def test_migrate_command(database_url, monkeypatch, capsys):
    monkeypatch.setenv("LOOPI_DATABASE_URL", database_url)
    assert cli.main(["migrate"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_pair_token_command(database_url, pki_settings, monkeypatch, capsys):
    monkeypatch.setenv("LOOPI_DATABASE_URL", database_url)
    monkeypatch.setenv("LOOPI_PKI_DIR", str(pki_settings.pki_dir))
    assert cli.main(["pair-token"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines[0]) == 9 and lines[0][4] == "-"
    assert lines[2].startswith("ca_fingerprint: ")
