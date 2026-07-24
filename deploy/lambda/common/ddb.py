# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DynamoDB helpers shared across Lambdas (T3-3)."""


def scan_all(table, **kwargs):
    """Fully scan a DynamoDB table, following LastEvaluatedKey (T2-6).

    A bare Table.scan() truncates at the 1 MB (~500-1000 row) page, so past
    that the sweep silently stops seeing rows. This loops until exhausted;
    all scan kwargs are forwarded to every page. No ExclusiveStartKey arg.

    This is the single authoritative copy of what was `_scan_all`, duplicated
    verbatim in the api / health_check / scaler / backup handlers.
    """
    items = []
    resp = table.scan(**kwargs)
    items.extend(resp.get("Items", []))
    while resp.get("LastEvaluatedKey"):
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"], **kwargs)
        items.extend(resp.get("Items", []))
    return items
