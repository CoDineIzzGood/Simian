from sklearn.ensemble import GradientBoostingClassifier

def train_gb(X, y, params=None):
    model = GradientBoostingClassifier(**(params or {}))
    model.fit(X, y)
    return model

def predict(model, X_input):
    return model.predict(X_input)