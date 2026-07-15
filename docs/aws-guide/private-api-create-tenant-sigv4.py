#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""#122 / #118 — 经私有 API Gateway(AWS_IAM 授权) + SigV4 签名创建租户的参考实现。

一条龙覆盖两件加固能力:
  1) 私有 API 加固:控制面 POST /tenants 走 PRIVATE endpoint + IAM 授权,调用方
     必须用 AWS 凭据对请求做 SigV4 签名(botocore SigV4Auth),否则 403。
  2) 凭据 KMS 加密注入(#118):把要注入租户 microVM 的凭据(如 AWS AKSK)先用
     ClawPool CMK 加密成密文(EncryptionContext 绑 owner_id),再随 create_tenant
     的 injected_credentials 提交。明文不进 API/DDB/日志;host 侧用自己的 role 解密。

用法(从私有 API 可达的网络内运行 —— 同 VPC 的 EC2 / VPCE 内;公网跑会连不上私有端点):

    pip install boto3 botocore requests
    export AWS_REGION=us-east-1
    # 要注入的凭据经环境变量传,绝不上命令行(命令行参数会落 shell history + 进程表,
    # 与本方案"明文不落命令行"红线冲突)。AWS 调用凭据走标准解析(env/~/.aws/实例角色);
    # 调用方 IAM 需 execute-api:Invoke + kms:Encrypt。
    export INJECT_AWS_ACCESS_KEY_ID=AKIAEXAMPLE...
    export INJECT_AWS_SECRET_ACCESS_KEY=wJalr...
    # 私有 API 双因子:SigV4(IAM 身份)+ x-api-key(openclaw-private-key 密钥门)。
    export PRIVATE_API_KEY=$(aws apigateway get-api-key --api-key <private-key-id> --include-value --query value --output text)
    python3 private-api-create-tenant-sigv4.py \
        --api-url   https://<api-id>.execute-api.us-east-1.amazonaws.com/v1 \
        --cmk-arn   arn:aws:kms:us-east-1:<acct>:key/<clawpool-cmk-id> \
        --owner-id  11111111-2222-3333-4444-555555555555 \
        --name      my-agent

CfnOutput 里拿参数:PrivateApiUrl(--api-url)、ClawPoolCmkArn(--cmk-arn)。
"""

import argparse
import json
import sys

import requests
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.session import Session


def encrypt_credential(kms_client, cmk_arn: str, owner_id: str, plaintext: str) -> str:
    """用 ClawPool CMK 把一条凭据值加密成 base64 密文。EncryptionContext 绑 owner_id
    —— 该密文只能在同一个 owner_id 下解密(host 侧用 owner_id 解),跨用户拿到也解不开。
    对齐 core/kms_envelope.py 的加密侧。"""
    import base64

    resp = kms_client.encrypt(
        KeyId=cmk_arn,
        Plaintext=plaintext.encode(),
        EncryptionContext={"owner_id": owner_id},
    )
    return base64.b64encode(resp["CiphertextBlob"]).decode()


def sigv4_post(api_url: str, region: str, body: str, api_key: str) -> requests.Response:
    """对 execute-api 的 POST /tenants 做 SigV4 签名并发送。

    双因子:SigV4(网络+IAM 身份)+ x-api-key(应用层密钥门)。私有 API 的 method
    既要 AWS_IAM 授权又要 api-key —— 因为控制面 handler 把"无 Bearer"请求当受信
    自动化 god-admin(靠 api-key 密钥兜),只挂 SigV4 会让任何可达 VPCE 的 IAM 主体
    变成无域全 fleet 管理员(安全评审 HIGH)。x-api-key 是 API Gateway 层单独校验的,
    不参与 SigV4 canonical request,所以签名后再加即可(加进签名也行,这里签名后加)。

    关键点(踩过的坑,见 memory private-apigw-sigv4-research):
      · service name 固定 'execute-api'(不是 'apigateway');region = API 部署 region。
      · body 只序列化一次,签名的 data 和发送的 body 必须同一份字节 —— 别用 requests
        的 json=(会重新序列化 → payload hash 不匹配 → 403)。
      · 临时凭据(STS)自带 .token,SigV4Auth 会自动注入 X-Amz-Security-Token。
    """
    endpoint = api_url.rstrip("/") + "/tenants"
    request = AWSRequest(
        method="POST",
        url=endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    credentials = Session().get_credentials()
    if credentials is None:
        sys.exit("no AWS credentials resolved (env / ~/.aws / instance role)")
    SigV4Auth(credentials, "execute-api", region).add_auth(request)
    headers = dict(request.headers)
    headers["x-api-key"] = api_key  # 私有 API 的应用层密钥门(与 SigV4 双因子)
    return requests.post(endpoint, headers=headers, data=body, timeout=30)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--api-url", required=True, help="PrivateApiUrl (CfnOutput)")
    p.add_argument(
        "--region", default=None, help="API region (default: AWS_REGION env)"
    )
    p.add_argument("--cmk-arn", required=True, help="ClawPoolCmkArn (CfnOutput)")
    p.add_argument(
        "--owner-id", required=True, help="平台用户 owner_id (EC 绑定 + 归属)"
    )
    p.add_argument("--name", default="sigv4-demo", help="租户名")
    args = p.parse_args()

    import os

    region = (
        args.region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
    )
    if not region:
        sys.exit("region required (--region or AWS_REGION)")

    # 要注入的凭据从环境变量读,绝不上命令行(命令行参数会落 shell history + 进程表)。
    aws_ak = os.environ.get("INJECT_AWS_ACCESS_KEY_ID")
    aws_sk = os.environ.get("INJECT_AWS_SECRET_ACCESS_KEY")
    if not aws_ak or not aws_sk:
        sys.exit(
            "set INJECT_AWS_ACCESS_KEY_ID + INJECT_AWS_SECRET_ACCESS_KEY env "
            "(never pass credentials on the command line)"
        )
    # 私有 API 的 x-api-key(openclaw-private-key)同样经 env 传,不上命令行。
    api_key = os.environ.get("PRIVATE_API_KEY")
    if not api_key:
        sys.exit(
            "set PRIVATE_API_KEY env (openclaw-private-key value; "
            "aws apigateway get-api-key --api-key <id> --include-value)"
        )

    import boto3

    kms_client = boto3.client("kms", region_name=region)

    # 1) 用 CMK 加密每条凭据(EncryptionContext=owner_id)。明文只在本进程,不出网。
    items = [
        {
            "name": "AWS_ACCESS_KEY_ID",
            "ciphertext": encrypt_credential(
                kms_client, args.cmk_arn, args.owner_id, aws_ak
            ),
        },
        {
            "name": "AWS_SECRET_ACCESS_KEY",
            "ciphertext": encrypt_credential(
                kms_client, args.cmk_arn, args.owner_id, aws_sk
            ),
        },
    ]

    # 2) 组 create_tenant body:owner_id(EC 绑定 + 代开归属)+ injected_credentials 密文。
    body = json.dumps(
        {
            "name": args.name,
            "owner_id": args.owner_id,
            "injected_credentials": {
                "kms_encrypted": True,
                "kms_key_arn": args.cmk_arn,
                "items": items,
            },
        }
    )

    # 3) SigV4 签名 + x-api-key + 经私有 API 提交。
    resp = sigv4_post(args.api_url, region, body, api_key)
    print(f"HTTP {resp.status_code}")
    try:
        print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
    except ValueError:
        print(resp.text)
    return 0 if resp.status_code in (200, 201, 202) else 1


if __name__ == "__main__":
    raise SystemExit(main())
