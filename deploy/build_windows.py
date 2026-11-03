"""Compatibility entry point; all desktop platforms share one native builder."""

from build_desktop import build

if __name__ == "__main__":
    build("windows")
