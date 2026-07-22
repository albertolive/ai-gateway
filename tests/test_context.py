"""Tests for scripts/context.py — dependency and docs resolution logic."""

import json

import context


class TestPackagesInDiff:
    def test_extracts_js_imports(self):
        deps = {"react": "18.0.0", "lodash": "4.17.0", "express": "4.18.0"}
        diff = "import React from 'react'\nimport _ from 'lodash'\n"
        found = context.packages_in_diff(diff, deps)
        assert "react" in found
        assert "lodash" in found

    def test_extracts_require_calls(self):
        deps = {"express": "4.18.0"}
        diff = "const express = require('express')\n"
        found = context.packages_in_diff(diff, deps)
        assert "express" in found

    def test_extracts_from_imports(self):
        deps = {"react": "18.0.0"}
        diff = "from 'react' import useState\n"
        found = context.packages_in_diff(diff, deps)
        assert "react" in found

    def test_skips_relative_imports(self):
        deps = {"react": "18.0.0"}
        diff = "import { foo } from './utils'\n"
        found = context.packages_in_diff(diff, deps)
        assert "react" not in found

    def test_extracts_scoped_packages(self):
        deps = {"@sveltejs/kit": "1.0.0", "react": "18.0.0"}
        diff = "import { redirect } from '@sveltejs/kit'\n"
        found = context.packages_in_diff(diff, deps)
        assert "@sveltejs/kit" in found

    def test_only_real_deps_kept(self):
        deps = {"react": "18.0.0"}
        diff = "import React from 'react'\nimport foo from 'nonexistent'\n"
        found = context.packages_in_diff(diff, deps)
        assert "react" in found
        assert "nonexistent" not in found

    def test_dedup(self):
        deps = {"react": "18.0.0"}
        diff = "import React from 'react'\nimport { useState } from 'react'\n"
        found = context.packages_in_diff(diff, deps)
        assert found.count("react") == 1

    def test_empty_diff(self):
        assert context.packages_in_diff("", {"react": "18.0.0"}) == []

    def test_empty_deps(self):
        assert context.packages_in_diff("import React from 'react'", {}) == []


class TestJsDeps:
    def test_reads_dependencies(self, tmp_path):
        pkg = {"dependencies": {"react": "18.0.0"},
               "devDependencies": {"jest": "29.0.0"}}
        (tmp_path / "package.json").write_text(json.dumps(pkg))
        deps = context._js_deps(str(tmp_path))
        assert deps["react"] == "18.0.0"
        assert deps["jest"] == "29.0.0"

    def test_missing_package_json(self, tmp_path):
        deps = context._js_deps(str(tmp_path))
        assert deps == {}

    def test_invalid_json(self, tmp_path):
        (tmp_path / "package.json").write_text("{invalid json")
        deps = context._js_deps(str(tmp_path))
        assert deps == {}

    def test_no_deps_sections(self, tmp_path):
        (tmp_path / "package.json").write_text(json.dumps({"name": "test"}))
        deps = context._js_deps(str(tmp_path))
        assert deps == {}


class TestReadCapped:
    def test_reads_full_file(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("Hello world")
        result = context._read_capped(str(f), 100)
        assert result == "Hello world"

    def test_truncates_at_cap(self, tmp_path):
        f = tmp_path / "big.md"
        content = "A" * 200
        f.write_text(content)
        result = context._read_capped(str(f), 50)
        assert len(result) <= 62  # 50 + "[truncated]"
        assert "[truncated]" in result

    def test_missing_file(self):
        assert context._read_capped("/nonexistent/file.md", 100) is None
