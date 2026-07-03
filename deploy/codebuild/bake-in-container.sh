#!/bin/bash
# 在 ubuntu 容器内跑(被 buildspec 的 docker run 调用)。装 debootstrap 依赖 + 跑
# build-rootfs.sh 烤黄金镜像。单独成脚本避免 buildspec YAML 里 docker run 的引号地狱。
# 入参经 env:ASSETS_BUCKET / IMAGE_VERSION / AWS_REGION。凭据靠 CodeBuild task role
# 透传(--network host + AWS_CONTAINER_CREDENTIALS_*)。
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
echo "=== [container] 装 build-rootfs 依赖 ==="
# sudo:build-rootfs.sh 有 41 处 sudo(为裸机/EC2 设计);容器内已是 root,装 sudo
# 让那些调用透传执行(sudo 在 root 下直接运行后续命令)。缺它 → line156 exit 127。
apt-get update -qq
apt-get install -y -qq sudo debootstrap e2fsprogs pigz curl ca-certificates unzip python3-venv >/dev/null
echo "=== [container] 装 awscli v2 (aarch64) ==="
curl -s "https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip" -o /tmp/a.zip
( cd /tmp && unzip -oq a.zip && ./aws/install >/dev/null )
echo "=== [container] 凭据自检(task role 透传) ==="
aws sts get-caller-identity --query Account --output text
echo "=== [container] 烤黄金镜像 ${IMAGE_VERSION} (含全部加固) ==="
cd /work
export REGION="${AWS_REGION}"
bash build-rootfs.sh "${IMAGE_VERSION}"
echo "=== [container] 烤制完成 ==="
