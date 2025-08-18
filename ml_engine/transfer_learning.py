from transformers import pipeline

def use_transfer_learning(text):
    classifier = pipeline("text-classification")
    return classifier(text)
