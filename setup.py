"""Build from the current source tree even when a checkout contains old wheels' staging files."""

from tempfile import TemporaryDirectory

from setuptools import setup

with TemporaryDirectory(prefix="pace-build-") as build_dir:
    setup(options={"build": {"build_base": build_dir}})
