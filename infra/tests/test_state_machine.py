"""The state machine's failure wiring.

Constraint 3 is a property of the synthesized ASL, not of the Python that
builds it, so these tests read the definition CloudFormation will deploy.

Two rules here cost real money when broken, and neither fails `cdk synth`:

* every state after the freeze must Catch to RollbackCredit, or a failed job
  strands the user's credit in `frozen` -- which is also what POST /generate
  rejects new jobs on, so the user is locked out permanently;
* FreezeCredit's InsufficientCreditsError catcher must come *before* its
  States.ALL catcher. Step Functions applies the first matching rule, so
  reversing them would refund a user who never had a credit to freeze.
"""

from __future__ import annotations

import shutil

import pytest

# See conftest: aws-cdk-lib shells out to node at import time.
if shutil.which("node") is None:  # pragma: no cover - environment guard
    pytest.skip("aws-cdk-lib needs node on PATH", allow_module_level=True)

from conftest import state_machine_definition

ROLLBACK = "RollbackCreditTask"
INSUFFICIENT_CREDITS = "InsufficientCreditsError"


@pytest.fixture(scope="module")
def definition(pipeline_stack) -> dict:
    return state_machine_definition(pipeline_stack)


@pytest.fixture(scope="module")
def states(definition) -> dict:
    return definition["States"]


def task_states(states: dict) -> dict:
    return {name: body for name, body in states.items() if body.get("Type") == "Task"}


# ----------------------------------------------------------------------
# Catch wiring
# ----------------------------------------------------------------------


def test_every_working_state_catches_to_rollback(states):
    """A state that fails without a Catch leaves the credit frozen forever."""
    working = [name for name in task_states(states) if name != ROLLBACK]
    assert working, "no task states found -- the definition parse is wrong"

    for name in working:
        catch_all = [c for c in states[name].get("Catch", []) if "States.ALL" in c["ErrorEquals"]]
        assert len(catch_all) == 1, f"{name} has no States.ALL catcher"
        assert catch_all[0]["Next"] == ROLLBACK, f"{name} does not catch to {ROLLBACK}"


def test_freeze_checks_insufficient_credits_before_the_catch_all(states):
    """Order is load bearing: the first matching rule wins, and refunding a
    freeze that never happened would credit a user who never paid."""
    catchers = states["FreezeCreditTask"]["Catch"]

    assert INSUFFICIENT_CREDITS in catchers[0]["ErrorEquals"]
    assert "States.ALL" in catchers[1]["ErrorEquals"]


def test_insufficient_credits_fails_without_refunding(states):
    """It must not route to rollback: nothing was frozen."""
    catcher = states["FreezeCreditTask"]["Catch"][0]

    assert catcher["Next"] != ROLLBACK
    assert states[catcher["Next"]]["Type"] == "Fail"


def test_rollback_fails_the_execution_after_refunding(states):
    """A refunded job is still a failed job; it must not reach Succeed."""
    assert states[ROLLBACK]["Type"] == "Task"
    assert states[states[ROLLBACK]["Next"]]["Type"] == "Fail"


def test_catch_preserves_the_payload_for_rollback(states):
    """rollback_credit validates a PipelineState off the event. Without
    result_path the catcher would replace the input with {Error, Cause} and the
    refund would fail on a payload that had lost its user_id."""
    for name in task_states(states):
        for catcher in states[name].get("Catch", []):
            if catcher["Next"] == ROLLBACK:
                assert catcher.get("ResultPath") == "$.error"


# ----------------------------------------------------------------------
# Timeout budget
# ----------------------------------------------------------------------


def worst_case_seconds(states: dict) -> int:
    """Longest a run can take: every task timing out through every retry.

    MaxAttempts counts retries *after* the first attempt, so a state with
    MaxAttempts=3 runs its timeout four times.

    Retriers are summed rather than maxed. Step Functions counts attempts per
    retrier, so a state whose failures alternate between error classes can
    exhaust each retrier in turn. Every state here has exactly one retrier
    today, which makes the two identical -- but a budget check that comes in
    under the real worst case is worse than no check at all.
    """
    total = 0
    for name, body in task_states(states).items():
        # A Task with no TimeoutSeconds runs effectively unbounded, which is
        # precisely what breaks the budget. Say so rather than KeyError.
        assert "TimeoutSeconds" in body, f"{name} has no task timeout"

        attempts, backoff = 1, 0
        for retry in body.get("Retry", []):
            retries = retry.get("MaxAttempts", 3)
            attempts += retries
            interval, rate = retry.get("IntervalSeconds", 1), retry.get("BackoffRate", 2.0)
            backoff += sum(int(interval * rate**i) for i in range(retries))
        total += attempts * body["TimeoutSeconds"] + backoff
    return total


def test_the_execution_budget_covers_the_worst_case_run(definition, states):
    """An execution-level timeout does NOT run any Catch -- the execution is
    terminated, rollback_credit never fires, and the credit stays frozen. So the
    budget has to exceed what the retry policies can actually spend, or those
    policies are unrunnable and the failure mode they exist for is unreachable.
    """
    assert definition["TimeoutSeconds"] >= worst_case_seconds(states)


def test_no_single_state_can_exhaust_the_budget_alone(definition, states):
    """The regression this guards: Synthesize's own 4 x 180s retry allowance
    once exceeded a 600s execution budget."""
    for name, body in task_states(states).items():
        spend = worst_case_seconds({name: body})
        assert spend < definition["TimeoutSeconds"], f"{name} alone can spend {spend}s"
