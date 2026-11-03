"""Run the real synthetic application's restore acceptance example from the checkout."""

from snapshot_proof.sample import run_demo
import json


if __name__ == "__main__":
    print(json.dumps(run_demo(), indent=2))
