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
        # Pass 1: check for duplicate step IDs and collect all IDs
        all_ids: set[str] = set()
        for step in self.steps:
            if step.id in all_ids:
                raise ValueError(f"Duplicate step id: '{step.id}'")
            all_ids.add(step.id)

        # Pass 2: check that all depends_on references exist
        for step in self.steps:
            for dep in step.depends_on:
                if dep not in all_ids:
                    raise ValueError(
                        f"Step '{step.id}' depends on '{dep}' which is not defined in this workflow"
                    )

        # Pass 3: cycle detection via Kahn's algorithm
        in_degree = {step.id: len(step.depends_on) for step in self.steps}
        dependents: dict[str, list[str]] = {step.id: [] for step in self.steps}
        for step in self.steps:
            for dep in step.depends_on:
                dependents[dep].append(step.id)

        queue = [sid for sid in in_degree if in_degree[sid] == 0]
        visited = 0
        while queue:
            current = queue.pop(0)
            visited += 1
            for dependent_id in dependents[current]:
                in_degree[dependent_id] -= 1
                if in_degree[dependent_id] == 0:
                    queue.append(dependent_id)

        if visited != len(self.steps):
            raise ValueError("Circular dependency detected among workflow steps")

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


class StartRunRequest(BaseModel):
    order_id: str | None = None


class RunSummaryResponse(BaseModel):
    id: str
    workflow_id: str
    workflow_name: str
    order_id: str | None
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
    order_id: str | None
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
