# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SSM Run Command helpers shared across Lambdas (T3-3).

Consolidates four drifting copies:
  * api `_ssm_send` (fire-and-forget, HOME-wrapped) — canonical form,
  * health_check `_ssm_send_hc` (byte-identical to _ssm_send),
  * api `_ssm_run` (blocking, HOME-wrapped, returns bool),
  * backup `_ssm_run` (blocking, NO HOME wrap, returns (bool, output)),
plus health_check's `_poll_ssm` (single non-blocking check) and
`_wait_ssm_done` (blocking wait returning (ok, err)).

Every function takes the module's `ssm_client` explicitly, so a Lambda's own
mocked client (and its `.exceptions.InvocationDoesNotExist`) stays the single
patch point the tests already use.
"""

import time

_HOME_WRAP = "export HOME=/home/ubuntu && cd /home/ubuntu && {cmd}"


def send(ssm_client, instance_id, command, timeout=120):
    """Fire-and-forget SSM Run Command. Returns the CommandId, or None on
    submission failure. Command is HOME-wrapped so `~` resolves to
    /home/ubuntu (SSM runs as root). Canonical form of api `_ssm_send` /
    health_check `_ssm_send_hc`."""
    try:
        resp = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [_HOME_WRAP.format(cmd=command)],
                        "executionTimeout": [str(timeout)]},
            TimeoutSeconds=timeout + 10,
        )
        return resp["Command"]["CommandId"]
    except Exception as e:
        print(f"SSM send error: {e}")
        return None


def run(ssm_client, instance_id, command, timeout=30, initial_sleep=3, poll_sec=2):
    """Submit a HOME-wrapped SSM command and block until it finishes.
    Returns (ok: bool, output: str) — the superset of api `_ssm_run` (which
    ignored output) and backup `_ssm_run`. `output` is StandardOutputContent
    on success, StandardErrorContent on failure, else a short reason.

    NOTE (T3-3): backup's old copy did NOT HOME-wrap; unified here it does.
    backup-data.sh is invoked by absolute path, so the wrap is inert there.
    """
    try:
        resp = ssm_client.send_command(
            InstanceIds=[instance_id],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": [_HOME_WRAP.format(cmd=command)],
                        "executionTimeout": [str(timeout)]},
            TimeoutSeconds=timeout + 10,
        )
        cmd_id = resp["Command"]["CommandId"]
        time.sleep(initial_sleep)  # let the invocation register
        for _ in range(max(1, timeout // 2)):
            try:
                result = ssm_client.get_command_invocation(
                    CommandId=cmd_id, InstanceId=instance_id)
            except ssm_client.exceptions.InvocationDoesNotExist:
                time.sleep(poll_sec)
                continue
            status = result["Status"]
            if status == "Success":
                return True, result.get("StandardOutputContent", "")
            if status in ("Failed", "TimedOut", "Cancelled"):
                err = result.get("StandardErrorContent", "")
                print(f"SSM {status}: {err[:200]}")
                return False, err
            time.sleep(poll_sec)
        print(f"SSM timeout waiting for command {cmd_id}")
        return False, "timeout"
    except Exception as e:
        print(f"SSM error: {e}")
        return False, str(e)


def poll(ssm_client, command_id, instance_id):
    """Single, non-blocking status check. Returns (done, ok):
      (False, _)    — pending / in-progress / not yet registered → re-check later
      (True, True)  — Success
      (True, False) — Failed / TimedOut / Cancelled
    Never blocks (unlike wait); the migration sweep needs 'still running' and
    'failed' kept distinct. Lift of health_check `_poll_ssm`."""
    try:
        inv = ssm_client.get_command_invocation(
            CommandId=command_id, InstanceId=instance_id)
    except ssm_client.exceptions.InvocationDoesNotExist:
        return False, False  # not registered yet; try next tick
    except Exception as e:
        print(f"ssm.poll error {command_id}/{instance_id}: {e}")
        return False, False
    status = inv.get("Status", "Pending")
    if status == "Success":
        return True, True
    if status in ("Failed", "TimedOut", "Cancelled"):
        print(f"ssm.poll {command_id}: {status} - "
              f"{(inv.get('StandardErrorContent') or '')[:200]}")
        return True, False
    return False, False  # Pending / InProgress / Delayed


def wait(ssm_client, command_id, instance_id, timeout_sec=90, poll_sec=3):
    """Block until an SSM command completes. Returns (ok, error_or_None). Lift
    of health_check `_wait_ssm_done`."""
    deadline = time.time() + timeout_sec
    last_status = "Pending"
    while time.time() < deadline:
        time.sleep(poll_sec)
        try:
            inv = ssm_client.get_command_invocation(
                CommandId=command_id, InstanceId=instance_id)
        except ssm_client.exceptions.InvocationDoesNotExist:
            continue
        except Exception as e:
            return False, f"get_command_invocation: {e}"
        last_status = inv.get("Status", "Unknown")
        if last_status == "Success":
            return True, None
        if last_status in ("Cancelled", "TimedOut", "Failed"):
            err = (inv.get("StandardErrorContent") or "")[:500]
            return False, f"SSM {last_status}: {err}"
        # else keep polling (InProgress / Pending / Delayed)
    return False, f"SSM timeout after {timeout_sec}s (last_status={last_status})"
