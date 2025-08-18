import lightgbm as lgb

def train_lgbm(X, y, params=None):
    model = lgb.LGBMClassifier(**(params or {}))
    model.fit(X, y)
    return model

def predict(model, X_input):
    return model.predict(X_input)