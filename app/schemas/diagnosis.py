from pydantic import BaseModel
from typing import List, Optional


class Prediction(BaseModel):
    label: str
    confidence: float


class DiagnosisResponse(BaseModel):
    predictions: List[Prediction]
    top: Prediction
    model: str
    processingMs: int
    scansRemaining: int
    historyId: Optional[int] = None
    gradcamBase64: Optional[str] = None
