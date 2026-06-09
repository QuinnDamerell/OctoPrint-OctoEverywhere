#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"

cd "${repo_root}"

skip_pyright=0
python_path_arg=""

while (($# > 0)); do
    case "$1" in
        --skip-pyright)
            skip_pyright=1
            ;;
        --python)
            shift
            if (($# == 0)); then
                echo "Error: --python requires a path argument." >&2
                exit 1
            fi
            python_path_arg="$1"
            ;;
        -h|--help)
            echo "Usage: ./developer/dev-tests.sh [--skip-pyright] [--python /path/to/python]"
            exit 0
            ;;
        *)
            echo "Error: unknown argument '$1'." >&2
            echo "Usage: ./developer/dev-tests.sh [--skip-pyright] [--python /path/to/python]" >&2
            exit 1
            ;;
    esac
    shift
done

if [[ -n "${python_path_arg}" ]]; then
    python_path="${python_path_arg}"
elif [[ -n "${VIRTUAL_ENV:-}" && -x "${VIRTUAL_ENV}/bin/python" ]]; then
    python_path="${VIRTUAL_ENV}/bin/python"
elif [[ -x "${HOME}/octoeverywhere-env/bin/python" ]]; then
    python_path="${HOME}/octoeverywhere-env/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    python_path="$(command -v python3)"
else
    echo "Error: unable to find a python interpreter. Activate a venv or pass --python." >&2
    exit 1
fi

if [[ ! -x "${python_path}" ]]; then
    echo "Error: python interpreter not found or not executable at '${python_path}'." >&2
    exit 1
fi

python_cmd=("${python_path}")

run_step() {
    echo ""
    echo "Running: $*"
    "$@"
}

ensure_lint_tools() {
    local missing_packages=()

    for package in pylint pyright ruff; do
        if ! "${python_cmd[@]}" -c "import ${package}" >/dev/null 2>&1; then
            missing_packages+=("${package}")
        fi
    done

    if ((${#missing_packages[@]} > 0)); then
        run_step "${python_cmd[@]}" -m pip install "${missing_packages[@]}"
    fi
}

pylint_targets=(
    ./octoeverywhere/
    ./octoprint_octoeverywhere/
    ./moonraker_octoeverywhere/
    ./elegoo_octoeverywhere/
    ./elegoo_cc2_octoeverywhere/
    ./bambu_octoeverywhere/
    ./prusalink_octoeverywhere/
    ./linux_host/
    ./py_installer/
    ./docker_octoeverywhere/
    ./setup.py
)

ensure_lint_tools

echo "Using Python: ${python_path}"

for target in "${pylint_targets[@]}"; do
    run_step "${python_cmd[@]}" -m pylint "${target}"
done

if ((skip_pyright == 0)); then
    run_step "${python_cmd[@]}" -m pyright --pythonpath "${python_path}"
else
    echo ""
    echo "Skipping pyright (--skip-pyright)."
fi

run_step "${python_cmd[@]}" -m ruff check
run_step "${python_cmd[@]}" -m unittest discover -s tests -v

echo ""
echo "All dev tests passed."
