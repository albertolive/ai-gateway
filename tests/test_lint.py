"""Tests for scripts/lint.py — static analysis layer."""

import lint


class TestSecretsScan:
    def test_detects_github_token(self, tmp_path):
        f = tmp_path / "config.py"
        f.write_text('token = "ghp_' + "A" * 36 + '"\n')
        findings = lint._secrets(str(tmp_path), ["config.py"])
        assert len(findings) == 1
        assert findings[0]["code"] == "github-token"

    def test_detects_aws_key(self, tmp_path):
        f = tmp_path / "settings.py"
        f.write_text('aws_key = "AKIA' + "B" * 16 + '"\n')
        findings = lint._secrets(str(tmp_path), ["settings.py"])
        assert len(findings) == 1
        assert findings[0]["code"] == "aws-access-key"

    def test_detects_private_key(self, tmp_path):
        f = tmp_path / "id_rsa"
        f.write_text("-----BEGIN RSA PRIVATE KEY-----\nfakekey\n")
        findings = lint._secrets(str(tmp_path), ["id_rsa"])
        assert len(findings) == 1
        assert findings[0]["code"] == "private-key"

    def test_detects_google_api_key(self, tmp_path):
        f = tmp_path / "gcloud.py"
        f.write_text('key = "AIza' + "C" * 35 + '"\n')
        findings = lint._secrets(str(tmp_path), ["gcloud.py"])
        assert len(findings) == 1
        assert findings[0]["code"] == "google-api-key"

    def test_detects_openai_key(self, tmp_path):
        f = tmp_path / "ai.py"
        f.write_text('api_key = "sk-' + "D" * 20 + '"\n')
        findings = lint._secrets(str(tmp_path), ["ai.py"])
        assert len(findings) == 1
        assert findings[0]["code"] == "openai-key"

    def test_detects_generic_secret(self, tmp_path):
        f = tmp_path / "env.py"
        f.write_text('password = "supersecretpassword123"\n')
        findings = lint._secrets(str(tmp_path), ["env.py"])
        assert len(findings) >= 1
        assert any(f["code"] == "generic-secret-assign" for f in findings)

    def test_no_false_positive_on_short_value(self, tmp_path):
        f = tmp_path / "config.py"
        f.write_text('password = "short"\n')
        findings = lint._secrets(str(tmp_path), ["config.py"])
        # "short" is 5 chars, below the 16-char minimum in the regex
        assert len(findings) == 0

    def test_one_finding_per_line(self, tmp_path):
        f = tmp_path / "multi.py"
        # Two different secrets on same line -> only one finding
        f.write_text('a = "ghp_' + "A" * 36 + '" b = "sk-' + "B" * 20 + '"\n')
        findings = lint._secrets(str(tmp_path), ["multi.py"])
        assert len(findings) == 1  # break after first match per line

    def test_missing_file_skipped(self, tmp_path):
        findings = lint._secrets(str(tmp_path), ["nonexistent.py"])
        assert findings == []


class TestChangedFiles:
    def test_basic(self):
        added = {"src/a.py": {1, 2}, "src/b.py": set(), "src/c.py": {5}}
        files = lint.changed_files(added)
        assert "src/a.py" in files
        assert "src/c.py" in files
        assert "src/b.py" not in files  # empty set = no added lines

    def test_empty(self):
        assert lint.changed_files({}) == []

    def test_sorted(self):
        added = {"z.py": {1}, "a.py": {1}, "m.py": {1}}
        files = lint.changed_files(added)
        assert files == ["a.py", "m.py", "z.py"]


class TestToPromptBlock:
    def test_renders_findings_on_changed_lines(self):
        added = {"src/main.py": {5, 10}}
        findings = [
            {"path": "src/main.py", "line": 5, "tool": "ruff",
             "code": "F401", "message": "Unused import"},
            {"path": "src/main.py", "line": 99, "tool": "ruff",
             "code": "F841", "message": "Unused var"},
        ]
        block = lint.to_prompt_block(findings, added)
        assert "F401" in block
        assert "Unused import" in block
        assert "F841" not in block  # line 99 not in added

    def test_secrets_always_included(self):
        added = {"src/main.py": {5}}
        findings = [
            {"path": "src/main.py", "line": 999, "tool": "secrets-scan",
             "code": "github-token", "message": "Hardcoded token"},
        ]
        block = lint.to_prompt_block(findings, added)
        assert "secrets-scan" in block
        assert "Hardcoded token" in block

    def test_empty_findings(self):
        assert lint.to_prompt_block([], {"src/main.py": {5}}) == ""

    def test_header_present(self):
        added = {"src/main.py": {5}}
        findings = [{"path": "src/main.py", "line": 5, "tool": "ruff",
                     "code": "F401", "message": "Unused"}]
        block = lint.to_prompt_block(findings, added)
        assert "Static analysis findings" in block
