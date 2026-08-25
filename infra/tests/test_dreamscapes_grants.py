"""IAM for the dreamscapes feature and the tagged-lifecycle split.

Two grants only show their absence at runtime: PutObject with a tag-set is
AccessDenied without PutObjectTagging (every generation would fail), and
rollback's sweep needs delete/list on jobs/. Both are asserted on the
synthesized template. Constraint 9 is asserted the other way round: no delete
grant anywhere in the pipeline reaches pictures/.
"""

from __future__ import annotations

import shutil

import pytest

if shutil.which("node") is None:  # pragma: no cover - environment guard
    pytest.skip("aws-cdk-lib needs node on PATH", allow_module_level=True)

from aws_cdk import assertions


def statements(template: assertions.Template, role_prefix: str) -> list[dict]:
    return [
        statement
        for name, policy in template.find_resources("AWS::IAM::Policy").items()
        if name.startswith(role_prefix)
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]
    ]


def actions(statement: dict) -> set[str]:
    action = statement.get("Action", [])
    return {action} if isinstance(action, str) else set(action)


@pytest.fixture(scope="module")
def template(pipeline_stack) -> assertions.Template:
    return assertions.Template.from_stack(pipeline_stack)


def test_generate_script_may_tag_what_it_uploads(template):
    tagging = [
        s for s in statements(template, "GenerateScript") if "s3:PutObjectTagging" in actions(s)
    ]
    assert len(tagging) == 1
    assert "s3:PutObject" in actions(tagging[0])
    assert "jobs/*" in str(tagging[0]["Resource"])


def test_rollback_may_sweep_jobs_and_only_jobs(template):
    rollback = statements(template, "RollbackCredit")
    delete = [s for s in rollback if "s3:DeleteObject" in actions(s)]
    listing = [s for s in rollback if "s3:ListBucket" in actions(s)]

    assert len(delete) == 1 and "jobs/*" in str(delete[0]["Resource"])
    assert len(listing) == 1
    assert listing[0]["Condition"] == {"StringLike": {"s3:prefix": "jobs/*"}}


TASK_ROLES = (
    "FreezeCredit",
    "DescribePicture",
    "GenerateScript",
    "Synthesize",
    "CommitCredit",
    "RollbackCredit",
)


def test_no_task_lambda_may_delete_pictures(template):
    """Constraint 9: uploaded pictures expire by lifecycle rule alone. The
    BucketDeployment handler (a deployment custodian with CDK's default
    read/write grant) is the documented exception and is not a task role."""
    for role in TASK_ROLES:
        for statement in statements(template, role):
            if any(a.startswith("s3:Delete") for a in actions(statement)):
                resource = str(statement["Resource"])
                assert "jobs/*" in resource and "pictures" not in resource, (role, resource)
