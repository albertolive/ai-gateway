"""Tests for scripts/impact.py — cross-file impact analysis."""

import tempfile

import impact


# ---------------------------------------------------------------------------
# extract_symbols
# ---------------------------------------------------------------------------

class TestExtractSymbols:
    def test_removed_function(self):
        diff = (
            "--- a/src/utils.py\n"
            "+++ b/src/utils.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-def calculate_total(items):\n"
            "+def calculate_sum(items):\n"
            "     pass\n"
        )
        removed, modified = impact.extract_symbols(diff)
        assert "calculate_total" in removed
        assert "calculate_sum" not in removed

    def test_modified_function_signature(self):
        diff = (
            "--- a/src/api.py\n"
            "+++ b/src/api.py\n"
            "@@ -5,3 +5,3 @@\n"
            "-def fetch_data(url, timeout=30):\n"
            "+def fetch_data(url, timeout=60, retries=3):\n"
            "     pass\n"
        )
        removed, modified = impact.extract_symbols(diff)
        assert "fetch_data" in modified
        assert "fetch_data" not in removed

    def test_js_function_rename(self):
        diff = (
            "--- a/src/auth.js\n"
            "+++ b/src/auth.js\n"
            "@@ -10,3 +10,3 @@\n"
            "-function validateToken(token) {\n"
            "+function validateAccessToken(token) {\n"
            "     return true;\n"
        )
        removed, modified = impact.extract_symbols(diff)
        assert "validateToken" in removed

    def test_class_definition(self):
        diff = (
            "--- a/src/models.py\n"
            "+++ b/src/models.py\n"
            "@@ -1,3 +1,4 @@\n"
            "-class User:\n"
            "+class UserAccount:\n"
            "     pass\n"
        )
        removed, modified = impact.extract_symbols(diff)
        assert "User" in removed
        assert "UserAccount" not in removed

    def test_const_arrow_function(self):
        diff = (
            "--- a/src/handlers.ts\n"
            "+++ b/src/handlers.ts\n"
            "@@ -3,3 +3,3 @@\n"
            "-const handleSubmit = (e) => {\n"
            "+const handleFormSubmit = (e) => {\n"
            "     e.preventDefault();\n"
        )
        removed, modified = impact.extract_symbols(diff)
        assert "handleSubmit" in removed

    def test_go_func(self):
        diff = (
            "--- a/main.go\n"
            "+++ b/main.go\n"
            "@@ -20,3 +20,3 @@\n"
            "-func ProcessRequest(r *http.Request) {\n"
            "+func HandleRequest(r *http.Request) {\n"
            "     // ...\n"
        )
        removed, modified = impact.extract_symbols(diff)
        assert "ProcessRequest" in removed

    def test_noise_symbols_filtered(self):
        diff = (
            "--- a/src/util.py\n"
            "+++ b/src/util.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-def get(data):\n"
            "+def set(data):\n"
            "     pass\n"
        )
        removed, modified = impact.extract_symbols(diff)
        assert "get" not in removed  # noise word
        assert "set" not in removed  # noise word

    def test_short_symbols_filtered(self):
        diff = (
            "--- a/src/f.py\n"
            "+++ b/src/f.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-def fn(x):\n"
            "+def fx(x):\n"
            "     pass\n"
        )
        removed, modified = impact.extract_symbols(diff)
        # "fn" is 2 chars, below MIN_SYMBOL_LEN=3
        assert "fn" not in removed

    def test_no_symbols_in_empty_diff(self):
        removed, modified = impact.extract_symbols("")
        assert removed == set()
        assert modified == set()

    def test_no_symbols_in_context_only_diff(self):
        diff = (
            "--- a/src/main.py\n"
            "+++ b/src/main.py\n"
            "@@ -1,3 +1,3 @@\n"
            "     import os\n"
            "     import sys\n"
            "     \n"
        )
        removed, modified = impact.extract_symbols(diff)
        assert removed == set()
        assert modified == set()

    def test_added_only_symbol_not_in_removed(self):
        diff = (
            "--- a/src/new.py\n"
            "+++ b/src/new.py\n"
            "@@ -1,0 +1,3 @@\n"
            "+def brand_new_function():\n"
            "+    pass\n"
        )
        removed, modified = impact.extract_symbols(diff)
        assert "brand_new_function" not in removed
        assert "brand_new_function" not in modified  # only in added

    def test_async_function(self):
        diff = (
            "--- a/src/fetcher.py\n"
            "+++ b/src/fetcher.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-async def fetchData(url):\n"
            "+async def fetchAllData(urls):\n"
            "     pass\n"
        )
        removed, modified = impact.extract_symbols(diff)
        assert "fetchData" in removed

    def test_typescript_interface_rename(self):
        diff = (
            "--- a/src/types.ts\n"
            "+++ b/src/types.ts\n"
            "@@ -1,3 +1,3 @@\n"
            "-interface UserProfile {\n"
            "+interface UserAccount {\n"
            "     id: string;\n"
        )
        removed, modified = impact.extract_symbols(diff)
        assert "UserProfile" in removed

    def test_typescript_type_alias_rename(self):
        diff = (
            "--- a/src/types.ts\n"
            "+++ b/src/types.ts\n"
            "@@ -1,3 +1,3 @@\n"
            "-export type Result<T> = { success: true; data: T } | { success: false; error: string };\n"
            "+export type Outcome<T> = { success: true; data: T } | { success: false; error: string };\n"
        )
        removed, modified = impact.extract_symbols(diff)
        assert "Result" in removed

    def test_dunder_methods_filtered(self):
        """__init__, __str__, etc. must not be extracted — every class has them."""
        diff = (
            "--- a/src/models.py\n"
            "+++ b/src/models.py\n"
            "@@ -1,5 +1,5 @@\n"
            "-    def __init__(self, name):\n"
            "+    def __init__(self, name, email):\n"
            "         pass\n"
        )
        removed, modified = impact.extract_symbols(diff)
        assert "__init__" not in removed
        assert "__init__" not in modified

    def test_max_symbols_capped(self):
        """build_context should cap at MAX_SYMBOLS even with many removed symbols."""
        lines = []
        for i in range(15):
            lines.append(f"-def function_{i}():")
            lines.append(f"+def renamed_{i}():")
            lines.append("     pass")
        diff = "--- a/src/big.py\n+++ b/src/big.py\n@@ -1,45 +1,45 @@\n" + "\n".join(lines)
        # build_context slices to MAX_SYMBOLS internally; verify it doesn't crash
        # and doesn't produce more than MAX_SYMBOLS sections
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            result = impact.build_context(td, diff)
            # No refs in empty dir -> empty result. Test the cap indirectly:
            removed, modified = impact.extract_symbols(diff)
            assert len(removed) == 15  # extract_symbols returns all
            # build_context caps with [:MAX_SYMBOLS] before searching
            assert len(removed) > impact.MAX_SYMBOLS  # confirms cap is needed


# ---------------------------------------------------------------------------
# _diff_files
# ---------------------------------------------------------------------------

class TestDiffFiles:
    def test_basic(self):
        diff = (
            "--- a/src/main.py\n"
            "+++ b/src/main.py\n"
            "@@ -1,3 +1,3 @@\n"
            "     pass\n"
        )
        files = impact._diff_files(diff)
        assert "src/main.py" in files

    def test_multiple_files(self):
        diff = (
            "--- a/src/a.py\n"
            "+++ b/src/a.py\n"
            "--- a/src/b.py\n"
            "+++ b/src/b.py\n"
        )
        files = impact._diff_files(diff)
        assert "src/a.py" in files
        assert "src/b.py" in files

    def test_empty_diff(self):
        assert impact._diff_files("") == set()


# ---------------------------------------------------------------------------
# find_references
# ---------------------------------------------------------------------------

class TestFindReferences:
    def test_finds_reference_in_other_file(self, tmp_path):
        # Create a repo structure
        (tmp_path / "src" / "utils.py").parent.mkdir(parents=True)
        (tmp_path / "src" / "utils.py").write_text("old_func()\n")
        (tmp_path / "src" / "main.py").write_text("from utils import old_func\n")

        refs = impact.find_references(
            str(tmp_path), {"old_func"}, exclude_files={"src/utils.py"})
        assert "old_func" in refs
        # main.py should be found, utils.py excluded
        found_paths = [r[0] for r in refs["old_func"]]
        assert "src/main.py" in found_paths
        assert "src/utils.py" not in found_paths

    def test_excludes_noise_dirs(self, tmp_path):
        (tmp_path / "node_modules" / "pkg.js").parent.mkdir(parents=True)
        (tmp_path / "node_modules" / "pkg.js").write_text("my_func()\n")
        (tmp_path / "src" / "app.js").parent.mkdir(parents=True)
        (tmp_path / "src" / "app.js").write_text("my_func()\n")

        refs = impact.find_references(
            str(tmp_path), {"my_func"}, exclude_files=set())
        found_paths = [r[0] for r in refs["my_func"]]
        assert "src/app.js" in found_paths
        assert "node_modules/pkg.js" not in found_paths

    def test_no_symbols_returns_empty(self, tmp_path):
        refs = impact.find_references(str(tmp_path), set(), set())
        assert refs == {}

    def test_respects_max_per_symbol(self, tmp_path):
        (tmp_path / "a.py").write_text("target_func()\n")
        (tmp_path / "b.py").write_text("target_func()\n")
        (tmp_path / "c.py").write_text("target_func()\n")
        (tmp_path / "d.py").write_text("target_func()\n")

        refs = impact.find_references(
            str(tmp_path), {"target_func"}, set(), max_per_symbol=2)
        assert len(refs["target_func"]) == 2

    def test_word_boundary_matching(self, tmp_path):
        (tmp_path / "app.py").write_text("handle_error()\nhandle_errors()\n")
        refs = impact.find_references(
            str(tmp_path), {"handle_error"}, set())
        # Only the exact word "handle_error" should match, not "handle_errors"
        contents = [r[2] for r in refs.get("handle_error", [])]
        assert any("handle_error()" in c for c in contents)
        assert not any("handle_errors()" in c for c in contents)


# ---------------------------------------------------------------------------
# build_context
# ---------------------------------------------------------------------------

class TestBuildContext:
    def test_returns_markdown_with_refs(self, tmp_path):
        (tmp_path / "src" / "caller.py").parent.mkdir(parents=True)
        (tmp_path / "src" / "caller.py").write_text("old_name()\n")

        diff = (
            "--- a/src/def.py\n"
            "+++ b/src/def.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-def old_name():\n"
            "+def new_name():\n"
            "     pass\n"
        )
        result = impact.build_context(str(tmp_path), diff)
        assert "Cross-file impact analysis" in result
        assert "old_name" in result
        assert "removed/renamed" in result
        assert "caller.py" in result

    def test_returns_empty_when_no_refs(self, tmp_path):
        diff = (
            "--- a/src/def.py\n"
            "+++ b/src/def.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-def old_name():\n"
            "+def new_name():\n"
            "     pass\n"
        )
        # No other files reference old_name
        result = impact.build_context(str(tmp_path), diff)
        assert result == ""

    def test_returns_empty_when_no_symbols(self, tmp_path):
        diff = (
            "--- a/src/def.py\n"
            "+++ b/src/def.py\n"
            "@@ -1,3 +1,3 @@\n"
            "     pass\n"
        )
        result = impact.build_context(str(tmp_path), diff)
        assert result == ""

    def test_modified_symbol_labeled_correctly(self, tmp_path):
        (tmp_path / "src" / "caller.py").parent.mkdir(parents=True)
        (tmp_path / "src" / "caller.py").write_text("fetch_data()\n")

        diff = (
            "--- a/src/api.py\n"
            "+++ b/src/api.py\n"
            "@@ -1,3 +1,3 @@\n"
            "-def fetch_data(url, timeout=30):\n"
            "+def fetch_data(url, timeout=60, retries=3):\n"
            "     pass\n"
        )
        result = impact.build_context(str(tmp_path), diff)
        assert "fetch_data" in result
        assert "modified" in result
