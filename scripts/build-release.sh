#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-${ROOT}/dist/release}"
BUILD_DIR="${REGISTRAR_BUILD_DIR:-${ROOT}/build/pyinstaller}"
export PYINSTALLER_CONFIG_DIR="${PYINSTALLER_CONFIG_DIR:-${BUILD_DIR}/cache}"
case "$(uname -s)" in Darwin) platform=darwin ;; Linux) platform=linux ;; *) exit 2 ;; esac
case "$(uname -m)" in arm64|aarch64) arch=arm64 ;; x86_64|amd64) arch=x86_64 ;; *) exit 2 ;; esac
mkdir -p "${OUTPUT_DIR}" "${BUILD_DIR}/dist" "${BUILD_DIR}/work" "${BUILD_DIR}/spec" "${PYINSTALLER_CONFIG_DIR}"
uv run --group freeze pyinstaller --noconfirm --onedir --clean \
  --paths "${ROOT}/src" --collect-submodules registrar --name registrar \
  --distpath "${BUILD_DIR}/dist" --workpath "${BUILD_DIR}/work/registrar" \
  --specpath "${BUILD_DIR}/spec" "${ROOT}/scripts/registrar_entry.py"
tar -C "${BUILD_DIR}/dist" -czf "${OUTPUT_DIR}/registrar-${platform}-${arch}.tar.gz" registrar
if [[ "${SKIP_SMOKE:-0}" != "1" ]]; then
  smoke_root="$(mktemp -d)"
  CI=1 XDG_DATA_HOME="${smoke_root}/data" XDG_CACHE_HOME="${smoke_root}/cache" \
    "${BUILD_DIR}/dist/registrar/registrar" --help >/dev/null
  rm -rf "${smoke_root}"
fi
