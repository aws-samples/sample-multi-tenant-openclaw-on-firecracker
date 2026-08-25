#!/usr/bin/env python3
"""Backfill the sparse rootfs query key without changing source fields."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json

import boto3


def query_value(item):
    # #593 —— 软删租户不进 gsi_rootfs_version:与删除路径一致(软删 REMOVE q_rootfs_version)。
    # 不判 status 的话,回填会按 rootfs_version 仍在把已软删的行重新写回投影键,让死行继续污染
    # 该分区,回填反而抵消了删除侧的清理。deleted → 期望 None → 下面 apply_row 会 REMOVE 掉它。
    if item.get("status") == "deleted":
        return None
    value = item.get("rootfs_version")
    if isinstance(value, str) and value and len(value.encode("utf-8")) <= 256:
        return value
    return None


def iter_rows(table):
    kwargs = {
        # status 是 DDB 保留字,用 #s 别名投影。
        "ProjectionExpression": "id, rootfs_version, q_rootfs_version, #s",
        "ExpressionAttributeNames": {"#s": "status"},
        "ConsistentRead": True,
    }
    while True:
        out = table.scan(**kwargs)
        yield from out.get("Items", [])
        key = out.get("LastEvaluatedKey")
        if not key:
            return
        kwargs["ExclusiveStartKey"] = key


def apply_row(table, row, expected):
    """条件化写:仅当行仍处于扫描时的 (status, rootfs_version) 才落笔。返回 "applied" 或 "conflict"。

    回填跑在活表上,scan 与 write 之间有窗口。#593 codex 复审两条竞态:
      · 只 guard status 不够 —— 写回的 q_rootfs_version 值取自 rootfs_version,并发换版
        (rebuild/采用:rootfs_version 与其孪生 q_rootfs_version 一起变,status 不变)会让本轮
        的陈旧值盖掉刚写好的新投影键。故【同时】guard rootfs_version:存在则等值比较,不存在则
        attribute_not_exists —— 任一被并发改动即 ConditionalCheckFailedException。
      · 冲突不能静默吞掉当成功 —— 返回 "conflict",由 main() 计数、汇报并让整轮 fail,运维据此
        重跑(回填幂等:重扫拿到一致新态自会收敛)。
    条件落空 = 行已在 scan 后被并发写改动 = 良性跳过,绝不用陈旧值覆盖并发写。
    """
    names = {}
    values = {}
    conds = []
    scanned_status = row.get("status")
    if isinstance(scanned_status, str):
        names["#s"] = "status"
        values[":cs"] = scanned_status
        conds.append("#s = :cs")
    if "rootfs_version" in row:
        values[":crv"] = row["rootfs_version"]
        conds.append("rootfs_version = :crv")
    else:
        conds.append("attribute_not_exists(rootfs_version)")

    if expected is None:
        update_expr = "REMOVE q_rootfs_version"
    else:
        update_expr = "SET q_rootfs_version = :value"
        values[":value"] = expected

    kwargs = {
        "Key": {"id": row["id"]},
        "UpdateExpression": update_expr,
        "ConditionExpression": " AND ".join(conds),
    }
    if names:
        kwargs["ExpressionAttributeNames"] = names
    if values:
        kwargs["ExpressionAttributeValues"] = values
    try:
        table.update_item(**kwargs)
        return "applied"
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return "conflict"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--table", default="openclaw-tenants")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-table")
    parser.add_argument("--confirm-account")
    parser.add_argument("--workers", type=int, default=32)
    args = parser.parse_args()
    if args.apply and args.confirm_table != args.table:
        raise SystemExit("--apply requires --confirm-table matching --table")
    if args.apply:
        account = boto3.client("sts", region_name=args.region).get_caller_identity()[
            "Account"
        ]
        if args.confirm_account != account:
            raise SystemExit(
                f"--apply requires --confirm-account {account} for the active account"
            )

    table = boto3.resource("dynamodb", region_name=args.region).Table(args.table)
    scanned = changed = set_count = removed_count = 0
    applied_count = conflict_count = 0
    futures = []

    def drain(fs):
        nonlocal applied_count, conflict_count
        for future in fs:
            if future.result() == "conflict":
                conflict_count += 1
            else:
                applied_count += 1

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for row in iter_rows(table):
            scanned += 1
            expected = query_value(row)
            if row.get("q_rootfs_version") == expected:
                continue
            changed += 1
            set_count += int(expected is not None)
            removed_count += int(expected is None)
            if args.apply:
                futures.append(pool.submit(apply_row, table, row, expected))
                if len(futures) >= 1000:
                    drain(futures)
                    futures.clear()
        drain(futures)
    print(
        json.dumps(
            {
                "scanned": scanned,
                "changed": changed,
                "set": set_count,
                "removed": removed_count,
                "applied": args.apply,
                "applied_count": applied_count,
                "conflicts": conflict_count,
            }
        )
    )
    # #593 codex 复审:并发冲突被跳过时整轮必须 fail-loud,否则"报绿但漏回填"。回填幂等,
    # 重跑会按一致新态收敛;非零退出让运维知道要再跑一次(而非误以为已全覆盖)。
    if args.apply and conflict_count:
        raise SystemExit(
            f"{conflict_count} row(s) changed concurrently and were skipped "
            "(not overwritten). Re-run to converge — backfill is idempotent."
        )


if __name__ == "__main__":
    main()
