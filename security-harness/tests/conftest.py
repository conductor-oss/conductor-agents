import os
import sys

# Make the worker packages importable (common/, dast/, ...).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "workers"))

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
