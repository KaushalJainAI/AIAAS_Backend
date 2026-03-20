from pydantic import BaseModel, Field
from typing import List, Optional, Any

class ModelVersion(BaseModel):
    major: Optional[int] = None
    minor: Optional[int] = None
    patch: Optional[int] = None

class ModelMetadata(BaseModel):
    raw: str
    normalized: str
    vendor: Optional[str] = None
    family: Optional[str] = None
    version: ModelVersion = Field(default_factory=ModelVersion)
    tags: List[str] = Field(default_factory=list)

class IntentMetadata(BaseModel):
    raw: str
    intent: str
    signals: List[str] = Field(default_factory=list)

class ActionStep(BaseModel):
    type: str
    target: Optional[str] = None
    tool_name: Optional[str] = None
    parameters: Optional[dict] = None

class PolicyMetadata(BaseModel):
    allow_tools: bool = True
    allow_code: bool = True
    allow_file_read: bool = True
    allow_file_write: bool = False
    sandbox: bool = True

class OrchestrationState(BaseModel):
    model: ModelMetadata
    request: IntentMetadata
    action: Optional[dict] = None # Placeholder for complex planning
    policy: PolicyMetadata = Field(default_factory=PolicyMetadata)
