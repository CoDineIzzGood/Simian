import xgboost as xgb

def train_xgboost(X, y, params=None):
    model = xgb.XGBClassifier(**(params or {}))
    model.fit(X, y)
    return model

def predict(model, X_input):
    return model.predict(X_input)
