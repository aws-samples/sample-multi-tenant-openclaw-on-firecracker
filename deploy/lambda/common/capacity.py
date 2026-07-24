# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Host capacity math — the single source of truth (T3-3).

Before this module the API scheduler and the AZ-failover placer computed
"does this VM fit on this host?" DIFFERENTLY:

  * API `_host_free`/`_host_fits` applied CPU/MEM_OVERCOMMIT_RATIO to totals,
    checked BOTH vCPU and memory, and enforced MAX_VMS_PER_HOST.
  * health_check `pick_target_host` used `total_vcpu - used_vcpu` with NO
    overcommit ratio, NO memory check at all, and NO MAX_VMS cap.

So AZ failover could place a VM on a memory-exhausted or VM-capped host the API
would reject (→ "migrated but won't boot"), and with a cpu ratio > 1 it wrongly
refused hosts the API would accept. All three call sites now go through here.

Every function takes the ratios / cap explicitly so behavior is a pure function
of its inputs (the callers pass their module-level env constants). int()
truncation matches the historical API math exactly.
"""


def allocatable(host, cpu_ratio, mem_ratio):
    """(allocatable_vcpu, allocatable_mem_mb) after overcommit is applied.

    A host that hasn't published total_vcpu / total_mem_mb yet is treated as
    zero capacity (→ won't fit → skipped) rather than raising: this math runs
    inside the AZ-failover sweep, which must never crash on one malformed row.
    """
    a_vcpu = int(int(host.get("total_vcpu", 0)) * cpu_ratio)
    a_mem = int(int(host.get("total_mem_mb", 0)) * mem_ratio)
    return a_vcpu, a_mem


def host_free(host, cpu_ratio, mem_ratio):
    """(free_vcpu, free_mem_mb) = allocatable minus what's booked.

    `used_vcpu` / `used_mem_mb` default to 0 (a freshly-registered host may not
    have published them yet), matching the historical API behavior.
    """
    a_vcpu, a_mem = allocatable(host, cpu_ratio, mem_ratio)
    return a_vcpu - int(host.get("used_vcpu", 0)), a_mem - int(host.get("used_mem_mb", 0))


def host_fits(host, vcpu_needed, mem_needed, cpu_ratio, mem_ratio, max_vms=0):
    """True if a VM of this size fits: enough free vCPU AND memory, AND — when
    max_vms > 0 — the host is below the absolute per-host VM ceiling (#77).

    This is the unified predicate: the API scheduler, the migrate capacity
    check, and AZ-failover target selection all agree on the verdict.
    """
    free_vcpu, free_mem = host_free(host, cpu_ratio, mem_ratio)
    if free_vcpu < vcpu_needed or free_mem < mem_needed:
        return False
    if max_vms and int(host.get("vm_count", 0)) >= max_vms:
        return False
    return True


def rank_hosts(hosts, vcpu_needed, mem_needed, *, cpu_ratio, mem_ratio,
               max_vms=0, exclude_ids=()):
    """Least-loaded-first ordering of hosts that fit, spreading load (#77).

    Sort key (-free_vcpu, -free_mem, instance_id): most free vCPU first, then
    most free mem, then a deterministic instance_id tie-break — the exact
    ordering API `_find_host` uses. Returns the fitting hosts in preference
    order (empty list if none fit); callers take [0] or iterate.
    """
    exclude = set(exclude_ids)
    ranked = []
    for h in hosts:
        if h.get("instance_id") in exclude:
            continue
        if not host_fits(h, vcpu_needed, mem_needed, cpu_ratio, mem_ratio, max_vms):
            continue
        free_vcpu, free_mem = host_free(h, cpu_ratio, mem_ratio)
        ranked.append((-free_vcpu, -free_mem, h["instance_id"], h))
    ranked.sort(key=lambda c: (c[0], c[1], c[2]))
    return [c[3] for c in ranked]
