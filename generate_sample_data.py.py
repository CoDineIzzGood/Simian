import pandas as pd
from pathlib import Path

df = pd.DataFrame({
    "feature1": [5.1, 4.9, 6.2, 5.9],
    "feature2": [3.5, 3.0, 3.4, 3.0],
    "feature3": [1.4, 1.4, 5.4, 5.1],
    "target":   [0, 0, 1, 1],
})

path = Path("data/datasets/sample_data.csv")
path.parent.mkdir(parents=True, exist_ok=True)
df.to_csv(path, index=False)
print(f"[Simian] Sample dataset created at: {path}")
