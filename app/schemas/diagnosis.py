
from pydantic import BaseModel
from typing import List


class Prediction(BaseModel):
    label: str
    confidence: float


class DiagnosisResponse(BaseModel):
    predictions: List[Prediction]
    top: Prediction
    model: str
    processingMs: int
    scansRemaining: int
