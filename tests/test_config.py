"""Configuration validation, especially the layouts that would cause data loss."""

from __future__ import annotations

import unittest

from tests.helpers import TempCaseMixin, write_config

from forge import config as config_mod
from forge.errors import ConfigError


class ValidLayout(TempCaseMixin):
    def test_a_sane_config_loads(self) -> None:
        cfg = config_mod.load(write_config(self.root))
        self.assertEqual(cfg.max_bitrate_kbps, 64)
        self.assertEqual(cfg.preferred_codec, "libfdk_aac")
        self.assertEqual(cfg.fallback_codec, "aac")
        self.assertTrue(cfg.safe_tag_writes)

    def test_directories_are_created(self) -> None:
        cfg = config_mod.load(write_config(self.root))
        for directory in (cfg.source_dir, cfg.output_dir, cfg.archive_dir, cfg.state_dir):
            self.assertTrue(directory.is_dir())

    def test_derived_paths(self) -> None:
        cfg = config_mod.load(write_config(self.root))
        self.assertEqual(cfg.db_path.parent, cfg.state_dir)
        self.assertEqual(cfg.lock_path.parent, cfg.state_dir)


class DangerousLayouts(TempCaseMixin):
    """Layouts where a run could discover and convert its own outputs."""

    def test_output_inside_source_is_refused(self) -> None:
        path = write_config(self.root, output_dir=self.root / "src" / "out")
        with self.assertRaises(ConfigError) as caught:
            config_mod.load(path)
        self.assertIn("must not be inside source_dir", str(caught.exception))

    def test_deeply_nested_output_is_refused(self) -> None:
        path = write_config(self.root, output_dir=self.root / "src" / "a" / "b" / "c")
        with self.assertRaises(ConfigError):
            config_mod.load(path)

    def test_archive_inside_source_is_refused(self) -> None:
        path = write_config(self.root, archive_dir=self.root / "src" / "arch")
        with self.assertRaises(ConfigError):
            config_mod.load(path)

    def test_output_equal_to_source_is_refused(self) -> None:
        path = write_config(self.root, output_dir=self.root / "src")
        with self.assertRaises(ConfigError):
            config_mod.load(path)

    def test_output_equal_to_archive_is_refused(self) -> None:
        shared = self.root / "shared"
        path = write_config(self.root, output_dir=shared, archive_dir=shared)
        with self.assertRaises(ConfigError):
            config_mod.load(path)


class BadValues(TempCaseMixin):
    def test_non_positive_bitrate(self) -> None:
        for value in (0, -5):
            with self.subTest(value=value):
                with self.assertRaises(ConfigError):
                    config_mod.load(write_config(self.root, max_bitrate=value))

    def test_non_numeric_bitrate(self) -> None:
        with self.assertRaises(ConfigError):
            config_mod.load(write_config(self.root, max_bitrate="sixty"))

    def test_negative_settle_values(self) -> None:
        with self.assertRaises(ConfigError):
            config_mod.load(write_config(self.root, settle_seconds=-1))

    def test_missing_paths_section(self) -> None:
        path = self.root / "bad.ini"
        path.write_text("[encoding]\nmax_bitrate = 64\n")
        with self.assertRaises(ConfigError):
            config_mod.load(path)

    def test_missing_required_path(self) -> None:
        path = self.root / "bad.ini"
        path.write_text(f"[paths]\nsource_dir = {self.root}/src\n")
        with self.assertRaises(ConfigError):
            config_mod.load(path)

    def test_missing_file(self) -> None:
        with self.assertRaises(ConfigError):
            config_mod.load(self.root / "nope.ini")

    def test_covers_without_a_directory(self) -> None:
        path = self.root / "c.ini"
        path.write_text(
            f"[paths]\nsource_dir = {self.root}/s\noutput_dir = {self.root}/o\n"
            f"archive_dir = {self.root}/a\n[artwork]\nextract_covers = true\n"
        )
        with self.assertRaises(ConfigError):
            config_mod.load(path)


class Overrides(TempCaseMixin):
    def test_values_are_replaced(self) -> None:
        cfg = config_mod.load(write_config(self.root))
        self.assertEqual(config_mod.with_overrides(cfg, max_bitrate_kbps=48).max_bitrate_kbps, 48)

    def test_none_is_ignored(self) -> None:
        cfg = config_mod.load(write_config(self.root))
        self.assertEqual(config_mod.with_overrides(cfg, max_bitrate_kbps=None).max_bitrate_kbps, 64)

    def test_overrides_are_revalidated(self) -> None:
        # An override must not be a way around the dangerous-layout checks.
        cfg = config_mod.load(write_config(self.root))
        with self.assertRaises(ConfigError):
            config_mod.with_overrides(cfg, output_dir=cfg.source_dir / "inside")


if __name__ == "__main__":
    unittest.main()
