import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score

from ml_engine.train_utils import (
    train_model,
    load_sample_data
)

models_to_try = ["xgboost", "lgbm", "gb"]  # You can add "transfer" or "distributed" later

def select_best_model(data=None, target_column="target"):
    if data is None:
        data = load_sample_data()

    X = data.drop(columns=[target_column])
    y = data[target_column]

    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=42)

    best_score = 0
    best_model = None
    model_scores = {}

    for model_name in models_to_try:
        model = train_model(model_name, X_train, y_train)
        preds = model.predict(X_test)
        score = accuracy_score(y_test, preds)
        model_scores[model_name] = score

        if score > best_score:
            best_score = score
            best_model = (model_name, model)

    print(f"[Simian] Model scores: {model_scores}")
    print(f"[Simian] Best model: {best_model[0]} with score {best_score}")
    return best_model
