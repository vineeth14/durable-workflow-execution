from typing import Any

from pydantic import BaseModel, Field, model_validator


# --- Workflow request models ---


class WorkflowStepConfig(BaseModel):
    action: str
    duration_seconds: float = 1.0
    fail_probability: float = Field(default=0.0, ge=0.0, le=1.0)
    max_retries: int = Field(default=0, ge=0)


class WorkflowStep(BaseModel):
    id: str
    type: str
    config: WorkflowStepConfig
    depends_on: list[str] = []


class CreateWorkflowRequest(BaseModel):
    name: str
    steps: list[WorkflowStep] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_steps(self) -> "CreateWorkflowRequest":
        seen_ids: set[str] = set()
        for step in self.steps:
            if step.id in seen_ids:
                raise ValueError(f"Duplicate step id: '{step.id}'")
            for dep in step.depends_on:
                if dep not in seen_ids:
                    raise ValueError(
                        f"Step '{step.id}' depends on '{dep}' which has not appeared earlier"
                    )
            seen_ids.add(step.id)
        return self


# --- Workflow response models ---


class WorkflowSummaryResponse(BaseModel):
    id: str
    name: str
    created_at: str


class WorkflowDetailResponse(BaseModel):
    id: str
    name: str
    definition: Any
    created_at: str


# --- Run response models ---


class RunSummaryResponse(BaseModel):
    id: str
    workflow_id: str
    workflow_name: str
    status: str
    started_at: str | None
    completed_at: str | None


class StepStateResponse(BaseModel):
    id: str
    step_id: str
    step_index: int
    status: str
    retry_count: int
    max_retries: int
    started_at: str | None
    completed_at: str | None
    error_message: str | None


class RunDetailResponse(BaseModel):
    id: str
    workflow_id: str
    workflow_name: str
    status: str
    started_at: str | None
    completed_at: str | None
    steps: list[StepStateResponse]


# --- Order models ---


class CreateOrderRequest(BaseModel):
    amount: float = Field(gt=0)


class OrderResponse(BaseModel):
    id: str
    status: str
    amount: float
    created_at: str
    updated_at: str
