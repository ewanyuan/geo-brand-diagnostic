"""Run the same executable acceptance on a native zip or tar.gz package."""

import argparse
import json
from pathlib import Path
from verify_windows_package import verify

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(verify(args.package.resolve(), args.output.resolve()), ensure_ascii=True))
