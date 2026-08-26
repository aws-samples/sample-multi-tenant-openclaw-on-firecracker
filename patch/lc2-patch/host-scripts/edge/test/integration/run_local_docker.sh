#!/usr/bin/env bash
# deploy/edge/test/integration/run_local_docker.sh
#
# 本机(mac/dev)跑 balancer_phase_integration.sh 的薄封装:起一个 redis 容器和一个
# openresty 容器,把仓库挂进去,然后在 openresty 容器里执行探针。断言实现只有一份,
# 在 balancer_phase_integration.sh 里;这里只负责搭环境。
# CI 不用这个文件 —— p2-edge-gate 本身就跑在 openresty/openresty:alpine 里,
# 直接加 `services: redis` 调探针即可(不需要 docker-in-docker)。
#
# 用法: bash deploy/edge/test/integration/run_local_docker.sh
# 退出码沿用探针:0 = 两条臂都符合期望;1 = 断言失败;2 = 环境 SKIP。

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
RUN_ID="$$"
NET="oc-edge-it-$RUN_ID"
EDGE_C="oc-edge-it-edge-$RUN_ID"
REDIS_C="oc-edge-it-redis-$RUN_ID"

cleanup() {
    docker rm -f "$EDGE_C" "$REDIS_C" >/dev/null 2>&1
    docker network rm "$NET" >/dev/null 2>&1
}
trap cleanup EXIT

command -v docker >/dev/null 2>&1 || { echo "SKIP: 没有 docker"; exit 2; }
docker info >/dev/null 2>&1 || { echo "SKIP: docker daemon 不可达"; exit 2; }

COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"

start_edge_container() { # start_edge_container <host-path-to-mount>
    docker rm -f "$EDGE_C" >/dev/null 2>&1
    docker run -d --name "$EDGE_C" --network "$NET" \
        -v "$1:/repo:ro" -w /repo \
        openresty/openresty:alpine sleep 3600 >/dev/null
    docker exec "$EDGE_C" test -f /repo/deploy/edge/nginx.conf
}

docker network create "$NET" >/dev/null
docker run -d --name "$REDIS_C" --network "$NET" redis:7-alpine >/dev/null

# Docker Desktop(mac)只对已加入 file sharing 的路径做真 bind mount;未共享时挂进去
# 会是空目录,后面每条断言都会 FAIL 并被误读成"代码坏了"。先自证,失败就把 deploy/edge
# 复制到 $HOME 下的 staging 目录再挂(worktree 常在 /tmp,而 /tmp 往往挂不进去)。
if ! start_edge_container "$REPO_ROOT"; then
    STAGE="${OC_EDGE_IT_WORKDIR:-$HOME/.cache/oc-edge-it}/stage-$RUN_ID"
    mkdir -p "$STAGE/deploy"
    cp -R "$REPO_ROOT/deploy/edge" "$STAGE/deploy/edge"
    echo "note: $REPO_ROOT 挂不进容器,改挂 staging 副本 $STAGE"
    start_edge_container "$STAGE" || {
        echo "SKIP: docker 无法 bind mount(Docker Desktop file sharing 未包含 $HOME?)"
        exit 2
    }
fi

# alpine base 缺 bash/gettext(envsubst);与 CI 的 before_script 装的是同一批。
docker exec "$EDGE_C" apk add --no-cache bash gettext >/dev/null 2>&1 \
    || { echo "SKIP: apk add bash gettext 失败(容器没网?)"; exit 2; }

REDIS_IP="$(docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$REDIS_C")"
for _ in $(seq 1 15); do
    docker exec "$REDIS_C" redis-cli PING >/dev/null 2>&1 && break
    sleep 1
done

# /repo 是只读挂载,探针要写 workdir,所以把 TMPDIR 指到容器内可写路径。
docker exec -e "REDIS_HOST=$REDIS_IP" -e REDIS_PORT=6379 -e TMPDIR=/tmp \
    -e "OC_PROBE_COMMIT=$COMMIT" "$EDGE_C" \
    bash /repo/deploy/edge/test/integration/balancer_phase_integration.sh
