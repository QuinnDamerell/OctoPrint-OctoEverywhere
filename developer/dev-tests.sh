#!/usr/bin/env bash

set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"

cd "${repo_root}"

python_path="${HOME}/octoeverywhere-env/bin/python"
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

for target in "${pylint_targets[@]}"; do
    run_step "${python_cmd[@]}" -m pylint "${target}"
done

run_step "${python_cmd[@]}" -m pyright --pythonpath "${python_path}"
run_step "${python_cmd[@]}" -m ruff check
run_step "${python_cmd[@]}" -m unittest discover -s tests -v

echo ""
echo "All dev tests passed."
