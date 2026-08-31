#!/bin/sh
set -eu

test "$(uname -s)" = "Linux"
test "$(uname -m)" = "x86_64"
test -S /var/run/docker.sock
test "$#" -eq 1

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
evaluator_root=$(CDPATH= cd -- "$script_dir/.." && pwd -P)
invocation_root=$(pwd -P)
config_arg=$1
case "$config_arg" in
    /*) config_path=$config_arg ;;
    *) config_path=$invocation_root/$config_arg ;;
esac
test -f "$config_path"
config_path=$(CDPATH= cd -- "$(dirname -- "$config_path")" && pwd -P)/$(basename -- "$config_path")
instrument_root=$(git -C "$(dirname -- "$config_path")" rev-parse --show-toplevel)
test "$(git -C "$instrument_root" rev-parse --show-toplevel)" = "$instrument_root"

runner_image=iab/fibsem-validation-runner:v1
docker build \
    --platform linux/amd64 \
    --network=none \
    --file "$evaluator_root/container/fibsem-validation-runner.Dockerfile" \
    --tag "$runner_image" \
    "$evaluator_root/container"

set -- \
    --mount type=bind,src="$instrument_root",dst="$instrument_root",readonly \
    --mount type=bind,src="$evaluator_root",dst="$evaluator_root",readonly \
    --env "EVALUATOR_REPO_PATH=$evaluator_root" \
    --env "IAB_RUN_CONFIG=$config_path"
if [ "${INSTANCES_REPO_PATH+x}" = x ]; then
    set -- "$@" --env "INSTANCES_REPO_PATH=$INSTANCES_REPO_PATH"
fi
repository_values=$(
    docker run --rm \
        --platform linux/amd64 \
        --network=none \
        --read-only \
        --env HOME=/tmp \
        --env "PYTHONPATH=$instrument_root/src" \
        "$@" \
        --workdir "$instrument_root" \
        "$runner_image" \
        -c 'from pathlib import Path; import os, yaml; from instrument_benchmark.environment import read_repository_path_values; i, e = read_repository_path_values(Path.cwd()); p = Path(os.environ["IAB_RUN_CONFIG"]); v = yaml.safe_load(p.read_text()); c = Path(v["openfibsem_checkout"]); o = (c if c.is_absolute() else p.parent / c).resolve(); print(i, e, o, sep="\n")'
)
instances_repo_path=$(printf '%s\n' "$repository_values" | sed -n '1p')
evaluator_repo_path=$(printf '%s\n' "$repository_values" | sed -n '2p')
openfibsem_repo_path=$(printf '%s\n' "$repository_values" | sed -n '3p')
test -n "$instances_repo_path"
test -n "$evaluator_repo_path"
test -n "$openfibsem_repo_path"
test -z "$(printf '%s\n' "$repository_values" | sed -n '4p')"
case "$instances_repo_path" in /*) ;; *) exit 2 ;; esac
case "$evaluator_repo_path" in /*) ;; *) exit 2 ;; esac
case "$openfibsem_repo_path" in /*) ;; *) exit 2 ;; esac
instances_repo_path=$(CDPATH= cd -- "$instances_repo_path" && pwd -P)
evaluator_repo_path=$(CDPATH= cd -- "$evaluator_repo_path" && pwd -P)
openfibsem_repo_path=$(CDPATH= cd -- "$openfibsem_repo_path" && pwd -P)
test "$evaluator_repo_path" = "$evaluator_root"

socket_gid=$(stat -c '%g' /var/run/docker.sock)
git_bin=$(command -v git)
git_exec_path=$(git --exec-path)
test -x "$git_bin"
test -d "$git_exec_path"
test "$(git -C "$instances_repo_path" rev-parse --show-toplevel)" = "$instances_repo_path"
test "$(git -C "$evaluator_repo_path" rev-parse --show-toplevel)" = "$evaluator_repo_path"
test "$(git -C "$openfibsem_repo_path" rev-parse --show-toplevel)" = "$openfibsem_repo_path"
git_libraries=$(
    ldd "$git_bin" | awk \
        '$2 == "=>" && $3 ~ /^\// && $3 !~ /\/libc\.so\./ { print $3 }
         $1 ~ /^\// && $1 !~ /\/ld-linux/ { print $1 }'
)
test -n "$git_libraries"
set -- \
    --mount type=bind,src="$git_bin",dst="$git_bin",readonly \
    --mount type=bind,src="$git_exec_path",dst="$git_exec_path",readonly
for git_library in $git_libraries; do
    test -f "$git_library"
    set -- "$@" \
        --mount type=bind,src="$git_library",dst="$git_library",readonly
done

docker run --rm \
    --platform linux/amd64 \
    --network=none \
    --user "$(id -u):$(id -g)" \
    --group-add "$socket_gid" \
    --env HOME=/tmp \
    --env "PYTHONPATH=$instrument_root/src" \
    --env "INSTANCES_REPO_PATH=$instances_repo_path" \
    --env "EVALUATOR_REPO_PATH=$evaluator_repo_path" \
    "$@" \
    --mount type=bind,src="$instrument_root",dst="$instrument_root" \
    --mount type=bind,src="$instances_repo_path",dst="$instances_repo_path",readonly \
    --mount type=bind,src="$evaluator_repo_path",dst="$evaluator_repo_path",readonly \
    --mount type=bind,src="$openfibsem_repo_path",dst="$openfibsem_repo_path",readonly \
    --mount type=bind,src=/tmp,dst=/tmp \
    --mount type=bind,src=/var/run/docker.sock,dst=/var/run/docker.sock \
    --workdir "$instrument_root" \
    "$runner_image" \
    "$evaluator_root/scripts/validate_fibsem_benchmark.py" \
    --instrument-root "$instrument_root" --config "$config_path"
