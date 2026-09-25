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
    gradcam_image: Optional[str] = None
    top_class_index: int = 0
    recommendations: Optional[str] = None
