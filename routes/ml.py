
from fastapi import APIRouter
from pydantic import BaseModel
from modules import ml_model

router = APIRouter()

class TextInput(BaseModel):
    text: str

@router.post("/ml/classify")
def classify_text(input_data: TextInput):
    result = ml_model.classify(input_data.text)
    return {"classification": result}
