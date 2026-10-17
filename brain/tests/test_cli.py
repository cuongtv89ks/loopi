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


def test_mcp_token_commands_print_the_token_once_and_list_without_it(database_url, monkeypatch, capsys):
    monkeypatch.setenv("LOOPI_DATABASE_URL", database_url)
    assert cli.main(["mcp-token", "create", "Claude Code"]) == 0
    token = capsys.readouterr().out.strip()
    assert token.startswith("loopi_") and "\n" not in token
    assert cli.main(["mcp-token", "list"]) == 0
    listed = capsys.readouterr().out
    [line] = [row for row in listed.splitlines() if row.endswith("Claude Code")]
    assert "live" in line and token not in listed
    token_id = line.split()[0]
    assert cli.main(["mcp-token", "revoke", token_id]) == 0
    assert cli.main(["mcp-token", "revoke", token_id]) == 1
    assert cli.main(["mcp-token", "create", " "]) == 2
