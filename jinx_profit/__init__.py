"""JINX profit-selection research package."""
from .features import FEATURE_NAMES, extract_deployment_features
from .model import chronological_split, train_profit_model, score_opportunity
