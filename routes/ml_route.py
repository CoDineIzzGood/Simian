from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from ml_engine.model_manager import select_best_model

router = APIRouter()

class InferenceRequest(BaseModel):
    features: list[float]  # e.g. [5.1, 4.9, 6.2]

@router.post("/ml/predict")
async def predict_best_model(request: InferenceRequest):
    try:
        model_name, model = select_best_model()  # Select best model dynamically
        prediction = model.predict([request.features])
        return {
            "model_used": model_name,
            "input": request.features,
            "prediction": int(prediction[0])
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
