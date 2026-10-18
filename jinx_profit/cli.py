"""Train/score the imported launch-profit model on actual labeled trades.

Usage: python -m jinx_profit.cli train trades.jsonl model.joblib
       python -m jinx_profit.cli score model.joblib deployment.json
"""
import argparse
import json
from pathlib import Path
from .features import extract_deployment_features
from .model import train_profit_model, score_opportunity

def main(argv=None):
    parser = argparse.ArgumentParser(description="JINX launch-time profit model")
    sub = parser.add_subparsers(dest="action", required=True)
    train = sub.add_parser("train")
    train.add_argument("input_jsonl")
    train.add_argument("output_model")
    score = sub.add_parser("score")
    score.add_argument("input_model")
    score.add_argument("input_transaction")
    args = parser.parse_args(argv)
    if args.action == "train":
        import joblib
        rows = [json.loads(line) for line in Path(args.input_jsonl).read_text().splitlines() if line.strip()]
        estimator, unseen = train_profit_model(rows)
        joblib.dump(estimator, args.output_model)
        print(json.dumps({"trained_on":len(rows)-len(unseen),"held_out":len(unseen),"model":args.output_model}))
    else:
        import joblib
        estimator = joblib.load(args.input_model)
        tx = json.loads(Path(args.input_transaction).read_text())
        print(json.dumps({"profitability_probability":score_opportunity(estimator, extract_deployment_features(tx))}))

if __name__ == "__main__":
    main()
