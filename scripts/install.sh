#!/bin/sh
# Install a single-file executable; no pip, root, or service activation required.
set -eu

prefix="${HOME}/.local"
while [ "$#" -gt 0 ]; do
    case "$1" in
        --prefix)
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                echo 'install.sh: --prefix requires a directory' >&2
                exit 2
            fi
            prefix="$2"
            shift 2
            ;;
        -h|--help)
            echo 'Usage: sh scripts/install.sh [--prefix DIRECTORY]'
            echo 'Default: ~/.local/bin/serverwatch (requires Python 3.10+)'
            exit 0
            ;;
        *)
            echo "install.sh: unknown argument: $1" >&2
            exit 2
            ;;
    esac
done

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 "$script_dir/build.py" --output "$prefix/bin/serverwatch"
printf 'Installed ServerWatch. Add %s/bin to PATH if needed.\n' "$prefix"
