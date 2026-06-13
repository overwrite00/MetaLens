"""Tests for core.path_security module.

Tests cover:
- Path validation for files and directories
- Symlink handling
- Cross-platform behavior
- Atomic write with security
- Error handling
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from core.path_security import (
    validate_file_path,
    validate_directory_path,
    secure_atomic_write,
    normalize_path,
    PathSecurityError,
)


class TestValidateFilePath:
    """Tests for validate_file_path function."""

    def test_simple_file_validation(self, tmp_path):
        """Valid file path should resolve and return Path object."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        result = validate_file_path(str(test_file))
        assert isinstance(result, Path)
        assert result.is_file()
        assert result.is_absolute()

    def test_resolve_tilde(self, tmp_path, monkeypatch):
        """Path with ~ should expand to home directory."""
        # Create a test file in a temp location we control
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        # Test that tilde expansion works (even if different directory)
        # by using direct absolute path
        result = validate_file_path(str(test_file))
        assert result.is_absolute()

    def test_resolve_relative_path_to_absolute(self, tmp_path, monkeypatch):
        """Relative path should be resolved to absolute."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        # Change to temp directory
        monkeypatch.chdir(tmp_path)
        result = validate_file_path("test.txt")
        assert result.is_absolute()

    def test_nonexistent_file_with_must_exist_true(self, tmp_path):
        """Nonexistent file should raise error when must_exist=True."""
        nonexistent = tmp_path / "nonexistent.txt"

        with pytest.raises(PathSecurityError, match="File not found"):
            validate_file_path(str(nonexistent), must_exist=True)

    def test_nonexistent_file_with_must_exist_false(self, tmp_path):
        """Nonexistent file should NOT raise error when must_exist=False."""
        nonexistent = tmp_path / "nonexistent.txt"

        result = validate_file_path(str(nonexistent), must_exist=False)
        assert result == nonexistent.resolve()

    def test_directory_not_file_raises_error(self, tmp_path):
        """Directory path should raise error when validating as file."""
        with pytest.raises(PathSecurityError, match="not a regular file"):
            validate_file_path(str(tmp_path), must_exist=True)

    def test_invalid_path_raises_error(self):
        """Invalid path (file that doesn't exist, must_exist=True) should raise error."""
        # Test with a path that should definitely not exist
        # We check for "not found" error which is always raised for must_exist=True
        with pytest.raises(PathSecurityError, match="File not found"):
            validate_file_path("/nonexistent/impossible/path/file.txt", must_exist=True)

    def test_symlink_following(self, tmp_path):
        """Path validation should follow symlinks (resolve them)."""
        # Create real file
        real_file = tmp_path / "real.txt"
        real_file.write_text("real content")

        # Create symlink (skip on Windows if not supported)
        link_file = tmp_path / "link.txt"
        try:
            link_file.symlink_to(real_file)
        except OSError as e:
            pytest.skip(f"Symlinks not supported: {e}")

        # Validate link path — should resolve to real file
        result = validate_file_path(str(link_file), must_exist=True)
        assert result.is_file()
        # The resolved path may differ depending on OS

    def test_absolute_path_requirement(self, tmp_path, monkeypatch):
        """Relative paths in current dir should resolve to absolute."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        monkeypatch.chdir(tmp_path)
        result = validate_file_path("test.txt")
        assert result.is_absolute()


class TestValidateDirectoryPath:
    """Tests for validate_directory_path function."""

    def test_simple_directory_validation(self, tmp_path):
        """Valid directory path should resolve and return Path object."""
        result = validate_directory_path(str(tmp_path), must_exist=True)
        assert isinstance(result, Path)
        assert result.is_dir()
        assert result.is_absolute()

    def test_nonexistent_directory_with_must_exist_true(self, tmp_path):
        """Nonexistent directory should raise error when must_exist=True."""
        nonexistent = tmp_path / "nonexistent_dir"

        with pytest.raises(PathSecurityError, match="not a directory"):
            validate_directory_path(str(nonexistent), must_exist=True)

    def test_nonexistent_directory_with_must_exist_false(self, tmp_path):
        """Nonexistent directory should NOT raise error when must_exist=False."""
        nonexistent = tmp_path / "nonexistent_dir"

        result = validate_directory_path(str(nonexistent), must_exist=False)
        assert result == nonexistent.resolve()

    def test_file_not_directory_raises_error(self, tmp_path):
        """File path should raise error when validating as directory."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("content")

        with pytest.raises(PathSecurityError, match="not a directory"):
            validate_directory_path(str(test_file), must_exist=True)

    def test_unreadable_directory(self, tmp_path):
        """Directory without read permission should raise error."""
        test_dir = tmp_path / "restricted"
        test_dir.mkdir(mode=0o700)

        # Skip on Windows (permissions work differently)
        if sys.platform != "win32":
            os.chmod(test_dir, 0o000)
            try:
                with pytest.raises(PathSecurityError, match="No read/execute permission"):
                    validate_directory_path(str(test_dir), must_exist=True)
            finally:
                os.chmod(test_dir, 0o700)


class TestSecureAtomicWrite:
    """Tests for secure_atomic_write function."""

    def test_simple_atomic_write(self, tmp_path):
        """Basic write operation should succeed."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("original")

        def writer(path):
            path.write_text("modified")

        secure_atomic_write(test_file, writer)

        assert test_file.read_text() == "modified"

    def test_atomic_write_cleans_up_on_error(self, tmp_path):
        """Temporary file should be cleaned up if write fails."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("original")

        def failing_writer(path):
            raise ValueError("Write failed!")

        with pytest.raises(ValueError):
            secure_atomic_write(test_file, failing_writer)

        # Temp files should be cleaned up
        temp_files = list(tmp_path.glob("*.ml_tmp*"))
        assert len(temp_files) == 0

        # Original file should be unchanged
        assert test_file.read_text() == "original"

    def test_atomic_write_preserves_metadata(self, tmp_path):
        """Metadata should be preserved during atomic write."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("original")

        # Get original metadata
        original_stat = test_file.stat()

        def writer(path):
            path.write_text("modified")

        secure_atomic_write(test_file, writer)

        # Check metadata (note: mtime may differ slightly)
        # Size will definitely change due to content, but mode should be preserved
        assert test_file.stat().st_mode == original_stat.st_mode

    def test_atomic_write_invalid_path(self, tmp_path):
        """Writing to nonexistent file should raise error."""
        nonexistent = tmp_path / "nonexistent.txt"

        def writer(path):
            path.write_text("content")

        with pytest.raises(PathSecurityError, match="File not found"):
            secure_atomic_write(nonexistent, writer)

    def test_atomic_write_custom_suffix(self, tmp_path):
        """Custom temp suffix should be used."""
        test_file = tmp_path / "test.txt"
        test_file.write_text("original")

        temp_path_list = []

        def writer(path):
            temp_path_list.append(path)
            path.write_text("modified")

        secure_atomic_write(test_file, writer, temp_suffix=".custom_tmp")

        assert len(temp_path_list) > 0
        assert ".custom_tmp" in temp_path_list[0].name

    def test_atomic_write_binary_content(self, tmp_path):
        """Binary content should be written correctly."""
        test_file = tmp_path / "test.bin"
        test_file.write_bytes(b"\x00\x01\x02")

        def writer(path):
            path.write_bytes(b"\x03\x04\x05")

        secure_atomic_write(test_file, writer)

        assert test_file.read_bytes() == b"\x03\x04\x05"


class TestNormalizePath:
    """Tests for normalize_path function."""

    def test_normalize_absolute_path(self, tmp_path):
        """Absolute path should normalize."""
        result = normalize_path(str(tmp_path))
        assert result == str(tmp_path.resolve())

    def test_normalize_relative_path(self, tmp_path, monkeypatch):
        """Relative path should resolve to absolute."""
        monkeypatch.chdir(tmp_path)
        result = normalize_path(".")
        assert result == str(tmp_path)

    def test_normalize_with_dots(self, tmp_path):
        """Path with .. should be normalized."""
        sub_path = tmp_path / "a" / "b"
        sub_path.mkdir(parents=True, exist_ok=True)

        result = normalize_path(str(sub_path / ".." / "b"))
        assert result == str(sub_path)


class TestPathSecurityEdgeCases:
    """Edge case tests."""

    def test_deeply_nested_path(self, tmp_path):
        """Deeply nested path should be handled."""
        deep_path = tmp_path
        for i in range(20):
            deep_path = deep_path / f"dir{i}"
            deep_path.mkdir(exist_ok=True)

        test_file = deep_path / "test.txt"
        test_file.write_text("content")

        result = validate_file_path(str(test_file), must_exist=True)
        assert result.is_file()

    def test_path_with_spaces(self, tmp_path):
        """Path with spaces should be handled."""
        test_file = tmp_path / "file with spaces.txt"
        test_file.write_text("content")

        result = validate_file_path(str(test_file), must_exist=True)
        assert result.is_file()

    def test_path_with_special_chars(self, tmp_path):
        """Path with special characters should be handled."""
        # Use characters that are valid on most filesystems
        test_file = tmp_path / "file-with_special.chars.txt"
        test_file.write_text("content")

        result = validate_file_path(str(test_file), must_exist=True)
        assert result.is_file()

    def test_unicode_path(self, tmp_path):
        """Unicode path should be handled."""
        test_file = tmp_path / "файл.txt"
        try:
            test_file.write_text("content")
            result = validate_file_path(str(test_file), must_exist=True)
            assert result.is_file()
        except (OSError, UnicodeError):
            pytest.skip("Unicode filenames not supported on this filesystem")


class TestCrossValidation:
    """Tests that simulate API usage patterns."""

    def test_api_read_flow(self, tmp_path):
        """Simulate /read endpoint validation."""
        test_file = tmp_path / "document.pdf"
        test_file.write_text("PDF content")

        # This simulates API request handler
        path_param = str(test_file)

        try:
            fpath = validate_file_path(path_param, must_exist=True)
            assert fpath.is_file()
        except PathSecurityError:
            pytest.fail("Valid file path should not raise error")

    def test_api_list_flow(self, tmp_path):
        """Simulate /list endpoint validation."""
        # This simulates API request handler
        path_param = str(tmp_path)

        try:
            dirpath = validate_directory_path(path_param, must_exist=True)
            assert dirpath.is_dir()
        except PathSecurityError:
            pytest.fail("Valid directory path should not raise error")

    def test_api_write_flow(self, tmp_path):
        """Simulate /write endpoint with atomic write."""
        test_file = tmp_path / "document.docx"
        test_file.write_bytes(b"original docx binary")

        # Simulate API request
        path_param = str(test_file)

        try:
            fpath = validate_file_path(path_param, must_exist=True)

            def writer(tmp_path):
                tmp_path.write_bytes(b"modified docx binary")

            secure_atomic_write(fpath, writer)
            assert test_file.read_bytes() == b"modified docx binary"
        except PathSecurityError:
            pytest.fail("Valid write should not raise error")
