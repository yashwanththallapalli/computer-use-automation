"""Agent-facing capability catalog.

The stretch goal: expose saved artifacts as callable capabilities an AI agent
can discover and invoke by name with typed args. This is deliberately a thin
layer -- it loads artifacts, publishes their *contract* (never their steps or
locators, which are an implementation detail an agent has no business seeing),
and calls cua.replay for the actual invocation. No new execution logic lives
here.

    uvicorn cua.catalog:app --port 8000

GET  /capabilities                          list, with the typed contract
GET  /capabilities/{id}                     one capability's full contract
GET  /capabilities/{id}/tool_schema          the same contract as an
                                              Anthropic-style tool definition,
                                              ready to drop into a `tools=[]`
                                              list for a real agent loop
POST /capabilities/{id}/invoke               run it against the live target
"""

from __future__ import annotations

import glob
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request as HTTPRequest
from pydantic import BaseModel

from ._appserver import running_bankapp
from .replay import ReplayOptions, replay
from .results import ReplayResult
from .schema import CapabilityArtifact, InputParam, OutputField, ParamType

CAP_DIR = Path(__file__).resolve().parent / "capabilities"

_JSON_TYPE = {
    ParamType.STRING: "string",
    ParamType.ENUM: "string",
    ParamType.DATE: "string",
    ParamType.BOOLEAN: "boolean",
    ParamType.INTEGER: "integer",
    ParamType.MONEY: "string",  # a decimal amount as text, never a float
}


def _load_catalog() -> dict[str, CapabilityArtifact]:
    """capability_id -> the highest version found on disk."""
    by_id: dict[str, CapabilityArtifact] = {}
    for path in glob.glob(str(CAP_DIR / "*.json")):
        art = CapabilityArtifact.load(path)
        current = by_id.get(art.capability_id)
        if current is None or art.version > current.version:
            by_id[art.capability_id] = art
    return by_id


_catalog = _load_catalog()


@asynccontextmanager
async def lifespan(app: FastAPI):
    with running_bankapp() as base_url:
        app.state.base_url = base_url
        yield


app = FastAPI(
    title="Capability Catalog",
    description="Agent-facing interface to this project's saved capabilities.",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# the contract an agent sees -- never the artifact's steps/locators
# --------------------------------------------------------------------------- #

class CapabilitySummary(BaseModel):
    capability_id: str
    version: int
    ref: str
    name: str
    description: str
    approval: str
    requires_approval_for_risky_steps: bool


class CapabilityCard(CapabilitySummary):
    input_params: list[InputParam]
    outputs: list[OutputField]
    success_criteria: str


class ToolSchema(BaseModel):
    name: str
    description: str
    input_schema: dict


class InvokeRequest(BaseModel):
    inputs: dict[str, str | int | float | bool]
    approve_risky: bool = False


def _summary(art: CapabilityArtifact) -> CapabilitySummary:
    return CapabilitySummary(
        capability_id=art.capability_id, version=art.version, ref=art.ref,
        name=art.name, description=art.description,
        approval=art.approval.value,
        requires_approval_for_risky_steps=bool(art.policy.risky_step_ids),
    )


def _card(art: CapabilityArtifact) -> CapabilityCard:
    return CapabilityCard(
        **_summary(art).model_dump(),
        input_params=art.input_params, outputs=art.outputs,
        success_criteria=art.success_checkpoint.description,
    )


def _tool_schema(art: CapabilityArtifact) -> ToolSchema:
    props: dict[str, dict] = {}
    required: list[str] = []
    for p in art.input_params:
        prop: dict = {"type": _JSON_TYPE[p.type], "description": p.description}
        if p.enum_values:
            prop["enum"] = p.enum_values
        if p.example is not None:
            prop["examples"] = [str(p.example)]
        props[p.name] = prop
        if p.required:
            required.append(p.name)
    return ToolSchema(
        name=art.capability_id, description=art.description,
        input_schema={"type": "object", "properties": props,
                      "required": required, "additionalProperties": False},
    )


def _get(capability_id: str) -> CapabilityArtifact:
    art = _catalog.get(capability_id)
    if art is None:
        raise HTTPException(404, f"no capability named {capability_id!r}. "
                            f"known: {sorted(_catalog)}")
    return art


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #

@app.get("/health")
def health():
    return {"status": "ok", "target": app.state.base_url,
            "capabilities": sorted(_catalog)}


@app.get("/capabilities", response_model=list[CapabilitySummary])
def list_capabilities():
    return [_summary(a) for a in _catalog.values()]


@app.get("/capabilities/{capability_id}", response_model=CapabilityCard)
def get_capability(capability_id: str):
    return _card(_get(capability_id))


@app.get("/capabilities/{capability_id}/tool_schema", response_model=ToolSchema)
def get_tool_schema(capability_id: str):
    return _tool_schema(_get(capability_id))


@app.post("/capabilities/{capability_id}/invoke", response_model=ReplayResult)
def invoke_capability(capability_id: str, body: InvokeRequest, http_request: HTTPRequest):
    art = _get(capability_id)
    # evidence_root defaults to "evidence" (real invocations write real
    # evidence) but tests override it on app.state so repeated test runs
    # don't pollute the published evidence directory
    evidence_root = getattr(http_request.app.state, "evidence_root", Path("evidence"))
    # synchronous and browser-driven, so this can take a few seconds -- a
    # plain `def` route runs in FastAPI's threadpool, not the event loop
    result = replay(art, dict(body.inputs),
                    ReplayOptions(approve_risky=body.approve_risky,
                                 evidence_root=evidence_root))
    return result
