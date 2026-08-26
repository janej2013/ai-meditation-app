"""The IAM resources a Bedrock invoke needs, shared by the two stacks that call
a model: the pipeline (script and picture steps) and the companion agent.

A cross-region inference profile requires permission on *both* the profile
ARN and the underlying foundation model in every region the profile can
route to -- granting only the profile fails at runtime with an opaque
AccessDenied, on the one step that costs money to reach. Profile ids are
prefixed with their geo ("au.", "apac.", "us.", "eu."); a bare model id needs
only the model ARN in the calling region.

The region lists mirror ``aws bedrock get-inference-profile --query
models[].modelArn`` and are what ``infra/tests/test_bedrock_resources.py``
pins.
"""

from __future__ import annotations

# Where each geo-prefixed profile may route. An unlisted geo falls through to
# the bare-model branch, which produces an ARN naming the whole profile id as
# a model and grants nothing on the profile itself -- adding a geo here is
# part of adopting a profile from it.
_PROFILE_REGIONS: dict[str, list[str]] = {
    # Australia-only routing: Sydney and Melbourne.
    "au": ["ap-southeast-2", "ap-southeast-4"],
    "apac": [
        "ap-southeast-1",
        "ap-southeast-2",
        "ap-northeast-1",
        "ap-northeast-2",
        "ap-south-1",
    ],
    "us": ["us-east-1", "us-east-2", "us-west-2"],
    "eu": ["eu-central-1", "eu-west-1", "eu-west-3", "eu-north-1"],
}

# Geos whose profiles may process a request outside Australia.
_OFFSHORE_GEOS = frozenset({"apac", "us", "eu", "global", "jp", "in"})


def bedrock_invoke_resources(
    region: str, account: str, model_id: str, *, allow_offshore: bool = True
) -> list[str]:
    """ARNs for ``bedrock:InvokeModel*`` on ``model_id`` from ``region``.

    ``allow_offshore=False`` refuses any profile that can leave Australia.
    The pipeline keeps the default: ``-c bedrock_model_id`` may point it at an
    ``apac.`` profile for capacity, a documented trade. The companion agent
    passes False -- a listener's words in a conversation stay in Australia,
    a product promise the runner enforces at runtime with the same rule
    (``agent.native.llm.converse.model_family``) and this enforces at synth.
    """
    geo, _, base_model = model_id.partition(".")
    if not allow_offshore and geo in _OFFSHORE_GEOS:
        raise ValueError(
            f"{model_id!r} is a cross-region profile that may leave Australia; "
            "the agent accepts an au. profile or a bare in-region model id"
        )
    regions = _PROFILE_REGIONS.get(geo)
    if regions is None:
        return [f"arn:aws:bedrock:{region}::foundation-model/{model_id}"]
    return [
        f"arn:aws:bedrock:{region}:{account}:inference-profile/{model_id}",
        *[f"arn:aws:bedrock:{r}::foundation-model/{base_model}" for r in regions],
    ]
