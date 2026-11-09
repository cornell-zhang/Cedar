# SPDX-License-Identifier: Apache-2.0
"""`cedar-toolchain` helper: info / path / exec."""

import json
import os
import sys

from . import bin_dir, manifest, share_dir


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print("usage: cedar-toolchain info | path | share | exec <tool> [args...]")
        return 0
    if args[0] == "info":
        print(json.dumps({"bin_dir": str(bin_dir()), "share_dir": str(share_dir()), "manifest": manifest()}, indent=2))
        return 0
    if args[0] == "path":
        print(bin_dir())
        return 0
    if args[0] == "share":
        print(share_dir())
        return 0
    if args[0] == "exec" and len(args) >= 2:
        exe = bin_dir() / args[1]
        if not exe.is_file():
            print(f"unknown tool {args[1]}", file=sys.stderr)
            return 2
        os.execv(str(exe), [str(exe)] + args[2:])
    print("unknown command", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
