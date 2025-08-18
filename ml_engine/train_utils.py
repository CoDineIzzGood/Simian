from ml_engine.xgboost_model import train_xgboost
from ml_engine.lgbm_model import train_lgbm
from ml_engine.gb_model import train_gb

def train_model(model_name, X, y, params=None):
    if model_name == "xgboost":
        return train_xgboost(X, y, params)
    elif model_name == "lgbm":
        return train_lgbm(X, y, params)
    elif model_name == "gb":
        return train_gb(X, y, params)
    else:
        raise ValueError(f"Unsupported model: {model_name}")

def load_sample_data():
    return pd.read_csv("data/datasets/sample_data.csv")