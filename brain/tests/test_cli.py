from loopi_brain import cli


def test_init_pki_command_prints_ca_fingerprint(tmp_path, capsys):
    assert cli.main(["init-pki", "--pki-dir", str(tmp_path), "--host", "brain.lan"]) == 0
    assert "CA fingerprint:" in capsys.readouterr().out
    assert cli.main(["init-pki", "--pki-dir", str(tmp_path), "--host", "brain.lan"]) == 1
