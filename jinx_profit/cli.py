"""Train/score the imported launch-profit model on actual labeled trades.

Usage: python -m jinx_profit.cli train trades.jsonl model.joblib
       python -m jinx_profit.cli score model.joblib deployment.json
"""
import argparse
import json
from pathlib import Path
from .features import FEATURE_NAMES, extract_deployment_features
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
    donor = sub.add_parser("donor-train")
    donor.add_argument("deployments_jsonl")
    donor.add_argument("trades_jsonl")
    donor.add_argument("output_model")
    args = parser.parse_args(argv)
    if args.action == "train":
        import joblib
        rows = [json.loads(line) for line in Path(args.input_jsonl).read_text().splitlines() if line.strip()]
        estimator, unseen = train_profit_model(rows)
        joblib.dump(estimator, args.output_model)
        print(json.dumps({"trained_on":len(rows)-len(unseen),"held_out":len(unseen),"model":args.output_model}))
    elif args.action == "donor-train":
        import joblib
        from .donor_d1ff1cult11 import read_jsonl, build_labeled_dataset, fit_donor_replica, donor_slot_comparison
        data = build_labeled_dataset(read_jsonl(args.deployments_jsonl), read_jsonl(args.trades_jsonl))
        estimator, holdout = fit_donor_replica(data)
        joblib.dump(estimator, args.output_model)
        features = [[r["features"][key] for key in FEATURE_NAMES] for r in holdout]
        selected = [p[1] >= 0.5 for p in estimator.predict_proba(features)]
        print(json.dumps({"model":args.output_model,"total_closed":len(data),
                          "holdout":len(holdout),"donor_slot_comparison":
                          donor_slot_comparison(holdout, selected)}))
    else:
        import joblib
        estimator = joblib.load(args.input_model)
        tx = json.loads(Path(args.input_transaction).read_text())
        print(json.dumps({"profitability_probability":score_opportunity(estimator, extract_deployment_features(tx))}))

if __name__ == "__main__":
    main()
